from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    TextBlock,
)

from aeview.harness import claude_code
from aeview.harness.base import AdapterError
from aeview.process import ProcResult

_REVIEW = {"verdict": "approve", "summary": "ok", "findings": [], "next_steps": []}


def _result(**overrides) -> ResultMessage:
    base = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "s",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 5, "output_tokens": 3},
        "structured_output": _REVIEW,
    }
    base.update(overrides)
    return ResultMessage(**base)


def _messages(result: ResultMessage, *, text: str = "reviewed") -> list:
    # A typical run: one assistant turn (transcript) followed by the terminal ResultMessage.
    return [AssistantMessage(content=[TextBlock(text=text)], model="claude-opus-4-8"), result]


@pytest.fixture
def capture_query(monkeypatch):
    """Mock the SDK boundary: replace claude_code.query with a stub async generator that captures
    the call (prompt + options) and yields canned SDK messages. Override captured['messages'] before
    the call to control output; defaults to a valid approve review."""
    captured: dict = {"messages": _messages(_result())}

    async def fake_query(*, prompt, options, transport=None):
        captured["prompt"] = prompt
        captured["options"] = options
        for message in captured["messages"]:
            yield message

    monkeypatch.setattr(claude_code, "query", fake_query)
    return captured


def _install_raising_query(monkeypatch, exc: BaseException) -> None:
    async def fake_query(*, prompt, options, transport=None):
        raise exc
        yield  # unreachable — present only so this is an async generator (the SDK's query type)

    monkeypatch.setattr(claude_code, "query", fake_query)


async def test_options_pin_read_only_sandbox_and_schema(capture_query, tmp_path):
    adapter = claude_code.ClaudeCodeAdapter()
    out = await adapter.run("REVIEW PROMPT", "sonnet", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"

    opts = capture_query["options"]
    # The two-layer read-only contract carried on the SDK options.
    assert opts.permission_mode == "dontAsk"
    assert opts.disallowed_tools == ["Edit", "Write", "NotebookEdit"]
    sandbox = json.loads(opts.settings)["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["filesystem"]["denyWrite"] == ["/"]
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    # Read-anywhere: the filesystem root is granted as readable so reviewers can read references
    # outside the repo (under dontAsk the Read tool is otherwise denied outside cwd). Live-verified.
    assert opts.add_dirs == ["/"]
    assert opts.extra_args.get("no-session-persistence", "MISSING") is None
    # Structured output + model wiring; prompt goes to query(prompt=...), not into options.
    assert opts.output_format["type"] == "json_schema"
    assert "verdict" in opts.output_format["schema"]["properties"]
    assert opts.model == "sonnet"
    assert opts.cwd == str(tmp_path)
    assert capture_query["prompt"] == "REVIEW PROMPT"


async def test_run_structured_delivers_the_given_schema(capture_query, tmp_path):
    # The generic path hands through whatever schema it's given (e.g. dedup), not the review schema.
    from aeview.schema import duplicate_groups_json_schema

    capture_query["messages"] = _messages(_result(structured_output={"duplicate_groups": []}))
    schema = duplicate_groups_json_schema()
    out = await claude_code.ClaudeCodeAdapter().run_structured(
        "P", schema, "opus", tmp_path, tmp_path / "log", timeout=5.0
    )
    delivered = capture_query["options"].output_format["schema"]
    assert "duplicate_groups" in delivered["properties"]
    assert "verdict" not in delivered.get("properties", {})
    assert out.payload == {"duplicate_groups": []}


async def test_thinking_maps_to_effort(capture_query, tmp_path):
    await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, tmp_path / "log", "xhigh")
    assert capture_query["options"].extra_args["effort"] == "xhigh"


async def test_default_thinking_omits_effort(capture_query, tmp_path):
    adapter = claude_code.ClaudeCodeAdapter()
    await adapter.run("p", "opus", tmp_path, tmp_path / "log", "default")
    assert "effort" not in capture_query["options"].extra_args
    await adapter.run("p", "opus", tmp_path, tmp_path / "log", None)
    assert "effort" not in capture_query["options"].extra_args


async def test_usage_and_cost_are_mapped(capture_query, tmp_path):
    capture_query["messages"] = _messages(
        _result(total_cost_usd=0.25, usage={"input_tokens": 100, "output_tokens": 40})
    )
    out = await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, tmp_path / "log")
    assert out.usage.input_tokens == 100
    assert out.usage.output_tokens == 40
    assert out.usage.cost_usd == 0.25


async def test_binary_override_threads_to_cli_path(capture_query, tmp_path):
    await claude_code.ClaudeCodeAdapter("/custom/claude").run(
        "p", "opus", tmp_path, tmp_path / "log"
    )
    assert capture_query["options"].cli_path == "/custom/claude"


async def test_default_adapter_leaves_cli_path_unset(capture_query, tmp_path):
    # No override → cli_path None → the SDK uses its own resolution (bundled binary, then PATH).
    await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, tmp_path / "log")
    assert capture_query["options"].cli_path is None


async def test_missing_binary_becomes_non_transient_adapter_error(monkeypatch, tmp_path):
    _install_raising_query(monkeypatch, CLINotFoundError("Claude Code not found"))
    with pytest.raises(AdapterError) as ei:
        await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, tmp_path / "log")
    assert ei.value.transient is False


async def test_process_error_rate_limit_is_transient(monkeypatch, tmp_path):
    _install_raising_query(monkeypatch, ProcessError("boom", exit_code=1, stderr="rate limit hit"))
    with pytest.raises(AdapterError) as ei:
        await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, tmp_path / "log")
    assert ei.value.transient is True


async def test_process_error_generic_is_non_transient(monkeypatch, tmp_path):
    _install_raising_query(
        monkeypatch, ProcessError("boom", exit_code=2, stderr="some other error")
    )
    with pytest.raises(AdapterError) as ei:
        await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, tmp_path / "log")
    assert ei.value.transient is False


async def test_timeout_fails_fast_and_closes_the_generator(monkeypatch, tmp_path):
    # asyncio.timeout fires while the generator is still producing -> non-transient (fail-fast).
    # The finally aclose() must run the generator's cleanup (PEP 533: an async-for won't close it
    # on exception) — that teardown is what kills the SDK subprocess, so assert it actually runs.
    closed = {"aclosed": False}

    async def slow_query(*, prompt, options, transport=None):
        try:
            await asyncio.sleep(10)
            yield  # pragma: no cover - never reached; the timeout fires first
        finally:
            closed["aclosed"] = True  # GeneratorExit raised by aclose() runs this

    monkeypatch.setattr(claude_code, "query", slow_query)
    with pytest.raises(AdapterError) as ei:
        await claude_code.ClaudeCodeAdapter().run(
            "p", "opus", tmp_path, tmp_path / "log", timeout=0.05
        )
    assert ei.value.transient is False
    assert closed["aclosed"] is True  # the generator (hence the subprocess) was torn down


async def test_unexpected_sdk_error_is_normalized_to_adapter_error(monkeypatch, tmp_path):
    # Any non-AdapterError from the SDK (malformed JSON, anyio/internal) must surface as a
    # non-transient AdapterError — run_dedup only catches AdapterError, so an unnormalized error
    # would abort the merge and strand the run non-terminal.
    _install_raising_query(monkeypatch, RuntimeError("anyio task group exploded"))
    with pytest.raises(AdapterError) as ei:
        await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, tmp_path / "log")
    assert ei.value.transient is False


async def test_run_writes_transcript_and_result_to_log(capture_query, tmp_path):
    log = tmp_path / "review.log"
    await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, log)
    text = log.read_text()
    assert "reviewed" in text  # the assistant transcript
    assert "--- result ---" in text  # the result summary footer


async def test_error_path_writes_an_error_log(monkeypatch, tmp_path):
    _install_raising_query(monkeypatch, CLINotFoundError("nope"))
    log = tmp_path / "review.log"
    with pytest.raises(AdapterError):
        await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, log)
    assert "--- error ---" in log.read_text()


def test_resolve_cli_override_executable_absolute_path(tmp_path):
    binpath = tmp_path / "claude"
    binpath.write_text("#!/bin/sh\n")
    binpath.chmod(0o755)  # which() requires it to be executable
    assert claude_code.ClaudeCodeAdapter()._resolve_cli(str(binpath)) == str(binpath)


def test_resolve_cli_override_non_executable_is_none(tmp_path):
    f = tmp_path / "claude"
    f.write_text("not exec")  # mode 644 — exists but not executable -> doctor must fail it
    assert claude_code.ClaudeCodeAdapter()._resolve_cli(str(f)) is None


def test_resolve_cli_override_command_name_resolves_via_path(monkeypatch):
    # A bare command-name override (not an absolute path) resolves via PATH, not a fail.
    monkeypatch.setattr(claude_code, "which", lambda b: f"/usr/bin/{b}")
    assert claude_code.ClaudeCodeAdapter()._resolve_cli("my-claude") == "/usr/bin/my-claude"


def test_resolve_cli_override_unresolvable_is_none(monkeypatch):
    monkeypatch.setattr(claude_code, "which", lambda b: None)
    assert claude_code.ClaudeCodeAdapter()._resolve_cli("/nope/claude") is None


def test_resolve_cli_default_resolves_a_real_binary():
    # No override → a resolvable bundled binary (a real installed-dep artifact). Don't couple to
    # the SDK's private layout name; assert it resolves to an existing path.
    resolved = claude_code.ClaudeCodeAdapter()._resolve_cli(None)
    assert resolved is not None and Path(resolved).exists()


def test_empty_override_normalizes_to_sdk_default():
    # An empty harnessBinaries entry must not become cli_path="" (which the SDK treats as a path);
    # it coerces to None so the SDK uses its own resolution.
    assert claude_code.ClaudeCodeAdapter("")._cli_path is None


def test_get_adapter_forwards_binary_override():
    # get_adapter threads the per-harness override into the constructed adapter: claude via
    # cli_path, codex/copilot as their binary (argv[0]).
    from aeview.harness import get_adapter

    assert get_adapter("claude-code", "/x/claude")._cli_path == "/x/claude"
    assert get_adapter("codex", "/x/codex").binary == "/x/codex"
    assert get_adapter("copilot", "/x/copilot").binary == "/x/copilot"


async def test_unexpected_transient_text_error_is_retried(monkeypatch, tmp_path):
    # An unexpected (non-ProcessError) error whose text looks transient is classified transient,
    # so the fan-out retries it rather than failing fast on a masked overload.
    _install_raising_query(monkeypatch, RuntimeError("service overloaded, please try again"))
    with pytest.raises(AdapterError) as ei:
        await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, tmp_path / "log")
    assert ei.value.transient is True


async def test_cancelled_error_is_not_swallowed(monkeypatch, tmp_path):
    # CancelledError is a BaseException, not Exception — the catch-all must let it propagate.
    _install_raising_query(monkeypatch, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, tmp_path / "log")


async def test_run_rejects_schema_invalid_structured_output(capture_query, tmp_path):
    capture_query["messages"] = _messages(_result(structured_output={"summary": "no verdict"}))
    with pytest.raises(AdapterError, match="schema validation"):
        await claude_code.ClaudeCodeAdapter().run("p", "opus", tmp_path, tmp_path / "log")


def test_interpret_rate_limit_status_is_transient():
    rm = _result(is_error=True, api_error_status=429, result="slow down")
    with pytest.raises(AdapterError) as ei:
        claude_code.ClaudeCodeAdapter()._interpret(rm, [])
    assert ei.value.transient is True


def test_interpret_auth_error_is_non_transient():
    rm = _result(is_error=True, api_error_status=401, result="could not load credentials")
    with pytest.raises(AdapterError) as ei:
        claude_code.ClaudeCodeAdapter()._interpret(rm, [])
    assert ei.value.transient is False


def test_interpret_transient_via_errors_text_without_status():
    rm = _result(is_error=True, api_error_status=None, errors=["overloaded, please try again"])
    with pytest.raises(AdapterError) as ei:
        claude_code.ClaudeCodeAdapter()._interpret(rm, [])
    assert ei.value.transient is True


def test_interpret_no_result_message_is_error():
    with pytest.raises(AdapterError, match="no result message"):
        claude_code.ClaudeCodeAdapter()._interpret(None, [])


def test_interpret_missing_structured_output_is_error():
    rm = _result(structured_output=None)
    with pytest.raises(AdapterError, match="structured_output"):
        claude_code.ClaudeCodeAdapter()._interpret(rm, [])


def test_classify_transient_timeout_vs_real_transient():
    from aeview.harness.base import classify_transient
    from aeview.process import TIMED_OUT

    assert classify_transient(TIMED_OUT, "x: timed out after 1s") is False  # fail-fast
    assert classify_transient(1, "rate limit hit") is True
    assert classify_transient(1, "some other error") is False


def test_preflight_ok_when_binary_resolves_and_authed(monkeypatch):
    # No override → resolves the SDK's bundled binary; mock the auth probe so no subprocess spawns.
    monkeypatch.setattr(claude_code, "run_sync", lambda *a, **k: ProcResult(0, "", ""))
    pf = claude_code.ClaudeCodeAdapter().preflight()
    assert pf.status == "ok"


def test_preflight_warns_when_auth_unverified(monkeypatch):
    monkeypatch.setattr(claude_code, "run_sync", lambda *a, **k: ProcResult(1, "", "not logged in"))
    pf = claude_code.ClaudeCodeAdapter().preflight()
    assert pf.status == "warn"


def test_preflight_fails_when_override_binary_missing():
    # An explicit override that doesn't exist can't be resolved -> fail (no auth probe attempted).
    pf = claude_code.ClaudeCodeAdapter("/nonexistent/claude/binary").preflight()
    assert pf.status == "fail"
