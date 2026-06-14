from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from openai_codex import ApprovalMode, Sandbox, ServerBusyError, TransportClosedError, TurnResult
from openai_codex.types import ReasoningEffort, ThreadTokenUsage, TurnStatus

from aeview.harness import codex, get_adapter
from aeview.harness.base import AdapterError
from aeview.process import ProcResult

_VALID = {"verdict": "approve", "summary": "ok", "findings": [], "next_steps": []}


def _usage(inp: int, out: int) -> ThreadTokenUsage:
    block = {
        "cachedInputTokens": 0,
        "inputTokens": inp,
        "outputTokens": out,
        "reasoningOutputTokens": 0,
        "totalTokens": inp + out,
    }
    return ThreadTokenUsage.model_validate({"last": block, "total": block})


def _result(final: str | None, *, usage: ThreadTokenUsage | None = None) -> TurnResult:
    return TurnResult(
        id="t1",
        status=TurnStatus.completed,
        error=None,
        started_at=None,
        completed_at=None,
        duration_ms=None,
        final_response=final,
        items=[],
        usage=usage,
    )


class _Controller:
    """Drives the faked codex SDK and exposes what the adapter passed to it."""

    def __init__(self, state: dict, captured: dict) -> None:
        self._state = state
        self.captured = captured

    def set_raw_result(self, result: TurnResult) -> None:
        self._state["result"] = result
        self._state["exc"] = None

    def set_exc(self, exc: BaseException) -> None:
        self._state["exc"] = exc

    def set_delay(self, seconds: float) -> None:
        self._state["run_delay"] = seconds

    def set_thread_start_delay(self, seconds: float) -> None:
        self._state["thread_start_delay"] = seconds


@pytest.fixture
def codex_sdk(monkeypatch):
    """Mock the codex SDK boundary (codex.AsyncCodex) with an offline fake. The adapter resolves
    the SDK's bundled binary, so Tier-1 tests intercept the SDK call itself. Default = a valid
    approve TurnResult with usage 100/20; the controller sets a raw result / exception / run delay
    and inspects the thread_start + run arguments and the close() count."""
    state: dict = {
        "result": _result(json.dumps(_VALID), usage=_usage(100, 20)),
        "exc": None,
        "run_delay": 0.0,
        "thread_start_delay": 0.0,
    }
    captured: dict = {"closed": 0, "instances": 0}

    class FakeThread:
        async def run(self, input, *, output_schema=None, effort=None):
            captured["input"] = input
            captured["output_schema"] = output_schema
            captured["effort"] = effort
            if state["run_delay"]:
                await asyncio.sleep(state["run_delay"])
            if state["exc"] is not None:
                raise state["exc"]
            return state["result"]

    class FakeAsyncCodex:
        def __init__(self, config):
            captured["config"] = config
            captured["instances"] += 1

        async def thread_start(self, **kwargs):
            captured["thread_kwargs"] = kwargs
            if state["thread_start_delay"]:
                await asyncio.sleep(state["thread_start_delay"])
            return FakeThread()

        async def close(self):
            captured["closed"] += 1

    monkeypatch.setattr(codex, "AsyncCodex", FakeAsyncCodex)
    return _Controller(state, captured)


async def test_codex_runs_read_only_constrained(codex_sdk, tmp_path):
    out = await codex.CodexAdapter().run("PROMPT", "gpt-5.5", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert out.usage.input_tokens == 100
    assert out.usage.output_tokens == 20
    assert out.usage.cost_usd == 0.0  # codex reports no USD cost

    tk = codex_sdk.captured["thread_kwargs"]
    assert tk["sandbox"] == Sandbox.read_only
    assert tk["approval_mode"] == ApprovalMode.deny_all
    assert tk["ephemeral"] is True
    assert tk["model"] == "gpt-5.5"
    assert tk["cwd"] == str(tmp_path)
    assert codex_sdk.captured["input"] == "PROMPT"  # prompt is the turn input
    assert codex_sdk.captured["closed"] == 1  # the client is torn down on the happy path too


async def test_run_structured_delivers_strict_form_of_given_schema(codex_sdk, tmp_path):
    # The generic path must strictify and deliver whatever schema it's handed (e.g. dedup); codex's
    # constrained decoding needs every property required and additionalProperties:false.
    from aeview.schema import duplicate_groups_json_schema

    await codex.CodexAdapter().run_structured(
        "P", duplicate_groups_json_schema(), "gpt-5.5", tmp_path, tmp_path / "log", timeout=5.0
    )
    schema = codex_sdk.captured["output_schema"]
    assert "duplicate_groups" in schema["properties"]
    assert schema["additionalProperties"] is False  # strictified
    assert "duplicate_groups" in schema["required"]


async def test_codex_maps_thinking_to_reasoning_effort(codex_sdk, tmp_path):
    await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log", "xhigh")
    assert codex_sdk.captured["effort"] == ReasoningEffort.xhigh


async def test_codex_default_thinking_leaves_effort_unset(codex_sdk, tmp_path):
    await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log", "default")
    assert codex_sdk.captured["effort"] is None


async def test_codex_rejects_invalid_thinking(codex_sdk, tmp_path):
    with pytest.raises(AdapterError, match="thinking"):
        await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log", "ultra")
    assert codex_sdk.captured["instances"] == 0  # fails before any SDK work


async def test_usage_none_yields_zero(codex_sdk, tmp_path):
    # result.usage is optional (the tokenUsage notification may not arrive) — must not crash.
    codex_sdk.set_raw_result(_result(json.dumps(_VALID), usage=None))
    out = await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log")
    assert out.usage.input_tokens == 0
    assert out.usage.output_tokens == 0
    assert out.usage.cost_usd == 0.0


async def test_binary_override_threads_to_codex_bin(codex_sdk, tmp_path):
    # settings.harnessBinaries["codex"] reaches CodexConfig.codex_bin. An absolute path that which
    # can't resolve is passed through verbatim (so the SDK fails loud rather than using the bundle).
    await codex.CodexAdapter("/custom/codex").run("p", "gpt-5.5", tmp_path, tmp_path / "log")
    assert codex_sdk.captured["config"].codex_bin == "/custom/codex"


async def test_no_override_uses_bundled(codex_sdk, tmp_path):
    await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log")
    assert codex_sdk.captured["config"].codex_bin is None  # None → the SDK's bundled binary


def test_empty_override_is_treated_as_bundled():
    assert codex.CodexAdapter("")._codex_bin is None


async def test_timeout_is_non_transient_and_closes_client(codex_sdk, tmp_path):
    # A per-review timeout is fail-fast AND must tear down the client: asyncio.timeout only unwinds
    # the asyncio side, so close() is what actually kills the codex subprocess/worker thread.
    codex_sdk.set_delay(10.0)
    with pytest.raises(AdapterError) as ei:
        await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log", None, 0.01)
    assert ei.value.transient is False
    assert "timed out" in str(ei.value)
    assert codex_sdk.captured["closed"] == 1  # client closed despite the timeout


@pytest.mark.parametrize(
    ("message", "transient"),
    [("the server is overloaded, try again", True), ("invalid request: bad model", False)],
)
async def test_failed_turn_runtime_error_classified(codex_sdk, tmp_path, message, transient):
    # A failed turn surfaces as RuntimeError(turn.error.message); classify transient by text so an
    # overload still retries but a hard error fails fast.
    codex_sdk.set_exc(RuntimeError(message))
    with pytest.raises(AdapterError) as ei:
        await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log")
    assert ei.value.transient is transient
    assert codex_sdk.captured["closed"] == 1  # client closed on the error path too


async def test_server_busy_error_is_transient(codex_sdk, tmp_path):
    codex_sdk.set_exc(ServerBusyError(-32000, "busy", "server_overloaded"))
    with pytest.raises(AdapterError) as ei:
        await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log")
    assert ei.value.transient is True  # is_retryable_error(ServerBusyError) is True


async def test_transport_closed_is_non_transient(codex_sdk, tmp_path):
    codex_sdk.set_exc(TransportClosedError("codex process closed stdout"))
    with pytest.raises(AdapterError) as ei:
        await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log")
    assert ei.value.transient is False  # a dead transport fails fast


async def test_non_json_final_is_error(codex_sdk, tmp_path):
    codex_sdk.set_raw_result(_result("not json at all", usage=_usage(1, 1)))
    with pytest.raises(AdapterError, match="not JSON"):
        await codex.CodexAdapter().run_structured("p", {}, "gpt-5.5", tmp_path, tmp_path / "log")


async def test_empty_final_message_is_error(codex_sdk, tmp_path):
    codex_sdk.set_raw_result(_result(None, usage=_usage(1, 1)))
    with pytest.raises(AdapterError, match="no final message"):
        await codex.CodexAdapter().run_structured("p", {}, "gpt-5.5", tmp_path, tmp_path / "log")


async def test_run_rejects_schema_invalid_final(codex_sdk, tmp_path):
    # _interpret only parses JSON; the review-shape validation lives in run().
    bad = json.dumps({"verdict": "maybe", "summary": "x", "findings": [], "next_steps": []})
    codex_sdk.set_raw_result(_result(bad, usage=_usage(1, 1)))
    with pytest.raises(AdapterError, match="schema validation"):
        await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log")


async def test_bad_binary_override_fails_fast(tmp_path):
    # No fixture: exercise the REAL SDK. A nonexistent codex_bin → resolve raises FileNotFoundError
    # → AdapterError (fail-fast), before any subprocess spawns.
    with pytest.raises(AdapterError, match="not found"):
        await codex.CodexAdapter("/nonexistent/codex/binary").run(
            "p", "gpt-5.5", tmp_path, tmp_path / "log", None, 30.0
        )


async def test_timeout_during_thread_start_is_non_transient(codex_sdk, tmp_path):
    # The timeout wraps startup too: a slow thread_start (not just run) still times out and closes.
    codex_sdk.set_thread_start_delay(10.0)
    with pytest.raises(AdapterError) as ei:
        await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log", None, 0.01)
    assert ei.value.transient is False
    assert codex_sdk.captured["closed"] == 1


async def test_bare_name_override_resolves_via_which(codex_sdk, tmp_path, monkeypatch):
    # A bare command-name override is resolved through which before reaching CodexConfig.codex_bin.
    monkeypatch.setattr(codex, "which", lambda b: "/resolved/codex" if b == "mycodex" else None)
    await codex.CodexAdapter("mycodex").run("p", "gpt-5.5", tmp_path, tmp_path / "log")
    assert codex_sdk.captured["config"].codex_bin == "/resolved/codex"


async def test_writes_success_log(codex_sdk, tmp_path):
    log = tmp_path / "review.log"
    await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, log)
    text = log.read_text()
    assert "--- result ---" in text
    assert '"status": "completed"' in text  # the TurnResult status
    assert "approve" in text  # the final_response (review JSON) is logged


async def test_writes_error_log_on_failure(codex_sdk, tmp_path):
    codex_sdk.set_exc(RuntimeError("boom"))
    log = tmp_path / "review.log"
    with pytest.raises(AdapterError):
        await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, log)
    assert "--- error ---" in log.read_text()


def test_sdk_call_kwargs_match_the_real_sdk():
    # The SDK boundary is faked elsewhere; pin the kwarg names the adapter passes against the REAL
    # SDK so an upgrade that renames one fails here rather than at runtime.
    import inspect

    from openai_codex import AsyncCodex, AsyncThread

    start = inspect.signature(AsyncCodex.thread_start).parameters
    for kw in ("sandbox", "approval_mode", "ephemeral", "model", "cwd"):
        assert kw in start, f"AsyncCodex.thread_start no longer accepts {kw}"
    run = inspect.signature(AsyncThread.run).parameters
    for kw in ("input", "output_schema", "effort"):
        assert kw in run, f"AsyncThread.run no longer accepts {kw}"


def test_preflight_no_override_resolves_bundled_and_probes_bounded(monkeypatch):
    # No override → resolve the bundled binary (need not be on PATH) and probe auth, bounded.
    calls: list = []

    def record(args, cwd=None, timeout=None):
        calls.append((args, timeout))
        return ProcResult(0, "", "")

    monkeypatch.setattr(codex, "run_sync", record)
    pf = codex.CodexAdapter().preflight()
    assert pf.status == "ok"
    assert len(calls) == 1
    args, timeout = calls[0]
    assert args[1:] == ["login", "status"]  # binary + auth subcommand
    assert args[0].endswith("codex")  # the resolved bundled binary path
    assert timeout is not None and timeout > 0  # the probe is bounded


def test_preflight_override_resolves_and_probes(monkeypatch):
    monkeypatch.setattr(codex, "which", lambda binary: f"/resolved{binary}")
    calls: list = []

    def record(args, cwd=None, timeout=None):
        calls.append(args)
        return ProcResult(0, "", "")

    monkeypatch.setattr(codex, "run_sync", record)
    pf = codex.CodexAdapter("/opt/codex").preflight()
    assert pf.status == "ok"
    assert calls == [["/resolved/opt/codex", "login", "status"]]  # the override path is probed


def test_preflight_non_executable_override_fails(monkeypatch):
    monkeypatch.setattr(codex, "which", lambda binary: None)  # override doesn't resolve
    pf = codex.CodexAdapter("/nope").preflight()
    assert pf.status == "fail"


def test_preflight_fails_when_bundle_unavailable(monkeypatch):
    # With no override, codex resolves ONLY its bundled binary (no PATH fallback) — if the bundle is
    # missing, doctor must FAIL (not silently pass via PATH), matching what the run path would do.
    import codex_cli_bin

    monkeypatch.setattr(codex_cli_bin, "bundled_codex_path", lambda: Path("/no/bundle/codex"))
    pf = codex.CodexAdapter().preflight()
    assert pf.status == "fail"


def test_preflight_unverified_auth_is_warn(monkeypatch):
    monkeypatch.setattr(codex, "run_sync", lambda *a, **k: ProcResult(1, "", ""))
    pf = codex.CodexAdapter().preflight()
    assert pf.status == "warn"


def test_codex_resolves_through_the_registry():
    adapter = get_adapter("codex")
    assert isinstance(adapter, codex.CodexAdapter)
    assert adapter.schema_support == "constrained"
    assert adapter.binary == "codex"


def test_get_adapter_forwards_binary_override():
    adapter = get_adapter("codex", "/x/codex")
    assert isinstance(adapter, codex.CodexAdapter)
    assert adapter._codex_bin == "/x/codex"
    assert adapter.binary == "/x/codex"
