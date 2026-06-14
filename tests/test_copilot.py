from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from copilot import SessionEvent, SessionEventType
from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.session_events import AssistantMessageData, AssistantUsageData

from aeview.harness import copilot, get_adapter
from aeview.harness.base import AdapterError

_VALID = {"verdict": "approve", "summary": "ok", "findings": [], "next_steps": []}


def _message_event(content: str) -> SessionEvent:
    return SessionEvent(
        data=AssistantMessageData(content=content, message_id="m1"),
        id=uuid.uuid4(),
        timestamp=datetime.now(UTC),
        type=SessionEventType.ASSISTANT_MESSAGE,
    )


def _usage_event(inp: int, out: int) -> SessionEvent:
    return SessionEvent(
        data=AssistantUsageData(model="gpt-5.4", input_tokens=inp, output_tokens=out),
        id=uuid.uuid4(),
        timestamp=datetime.now(UTC),
        type=SessionEventType.ASSISTANT_USAGE,
    )


class _Controller:
    """Drives the faked copilot SDK and exposes what the adapter passed to it."""

    def __init__(self, state: dict, captured: dict) -> None:
        self._state = state
        self.captured = captured

    def queue_turn(self, answer, *, usage=(100, 20), exc=None, delay=0.0) -> None:
        """Queue one send_and_wait turn: `answer` is the assistant message text (None = no
        message), `usage` is one (input, output) tuple OR a list of them (multiple usage events),
        `exc` makes send_and_wait raise, `delay` sleeps before answering (for timeout/cancel)."""
        events = usage if isinstance(usage, list) else [usage]
        self._state["turns"].append({"answer": answer, "usage": events, "exc": exc, "delay": delay})

    def set_client_init_exc(self, exc: BaseException) -> None:
        # Simulate the real SDK raising in CopilotClient.__init__ (e.g. "Copilot CLI not found").
        self._state["client_init_exc"] = exc

    def set_stop_error(self, exc: BaseException) -> None:
        # Make client.stop() raise so _teardown_client must fall back to force_stop().
        self._state["stop_error"] = exc


@pytest.fixture
def copilot_sdk(monkeypatch):
    """Mock the copilot SDK boundary (copilot.CopilotClient) with an offline fake. The adapter
    resolves the SDK's bundled binary, so Tier-1 tests intercept the SDK call itself. Default = a
    valid approve answer with usage 100/20; queue_turn sets per-turn answer/usage/exc/delay and the
    controller exposes the client + session kwargs, the send_and_wait calls, and teardown counts
    (start/stop/force_stop on the client, disconnect on the session — explicit lifecycle, no
    context managers)."""
    state: dict = {"turns": [], "client_init_exc": None, "stop_error": None}
    captured: dict = {
        "clients": 0,
        "started": 0,
        "stopped": 0,
        "force_stopped": 0,
        "disconnected": 0,
        "client_kwargs": None,
        "create_kwargs": None,
        "send_calls": [],
        "thread_name": None,
    }

    def _next_turn() -> dict:
        if state["turns"]:
            return state["turns"].pop(0)
        return {"answer": json.dumps(_VALID), "usage": [(100, 20)], "exc": None, "delay": 0.0}

    class FakeSession:
        def __init__(self, create_kwargs: dict) -> None:
            self._handlers: list = []
            captured["create_kwargs"] = create_kwargs

        async def disconnect(self):
            captured["disconnected"] += 1

        def on(self, handler):
            self._handlers.append(handler)
            return lambda: self._handlers.remove(handler) if handler in self._handlers else None

        async def send_and_wait(self, prompt, *, timeout=60.0, **kwargs):
            captured["send_calls"].append({"prompt": prompt, "timeout": timeout})
            captured["thread_name"] = threading.current_thread().name
            turn = _next_turn()
            if turn["delay"]:
                await asyncio.sleep(turn["delay"])
            for inp, out in turn["usage"]:
                event = _usage_event(inp, out)
                for handler in list(self._handlers):
                    handler(event)
            if turn["exc"] is not None:
                raise turn["exc"]
            return None if turn["answer"] is None else _message_event(turn["answer"])

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured["client_kwargs"] = kwargs
            captured["clients"] += 1
            if state["client_init_exc"] is not None:
                raise state["client_init_exc"]

        async def start(self):
            captured["started"] += 1

        async def stop(self):
            captured["stopped"] += 1
            if state["stop_error"] is not None:
                raise state["stop_error"]

        async def force_stop(self):
            captured["force_stopped"] += 1

        async def create_session(self, **kwargs):
            return FakeSession(kwargs)

    monkeypatch.setattr(copilot, "CopilotClient", FakeClient)
    return _Controller(state, captured)


# --- SDK contract / read-only ----------------------------------------------------------


async def test_copilot_runs_read_only_via_sdk(copilot_sdk, tmp_path):
    out = await copilot.CopilotAdapter().run("PROMPT", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert out.usage.input_tokens == 100
    assert out.usage.output_tokens == 20
    assert out.usage.cost_usd == 0.0  # copilot reports no USD cost

    ck = copilot_sdk.captured["create_kwargs"]
    assert ck["model"] == "gpt-5.4"
    assert ck["working_directory"] == str(tmp_path)
    assert callable(ck["on_permission_request"])  # the read-only enforcement boundary
    assert copilot_sdk.captured["client_kwargs"]["working_directory"] == str(tmp_path)
    assert copilot_sdk.captured["client_kwargs"]["connection"] is None  # no override → bundled
    assert "PROMPT" in copilot_sdk.captured["send_calls"][0]["prompt"]  # prompt + embedded schema
    assert copilot_sdk.captured["stopped"] == 1  # client torn down on the happy path
    assert copilot_sdk.captured["disconnected"] == 1  # session disconnected too


async def test_permission_handler_approves_read_denies_everything_else(copilot_sdk, tmp_path):
    # Deny-by-default: ONLY kind="read" is approved (any path); every other kind is rejected.
    await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    handler = copilot_sdk.captured["create_kwargs"]["on_permission_request"]

    read = handler(SimpleNamespace(kind="read", path="/etc/passwd"), {})
    assert isinstance(read, PermissionDecisionApproveOnce)  # reads anywhere are allowed
    for kind in ("write", "shell", "url", "mcp", "memory", "hook", "custom-tool"):
        decision = handler(SimpleNamespace(kind=kind), {})
        assert isinstance(decision, PermissionDecisionReject), f"{kind} must be denied"


async def test_schema_embedded_in_prompt(copilot_sdk, tmp_path):
    await copilot.CopilotAdapter().run("REVIEW", "gpt-5.4", tmp_path, tmp_path / "log")
    sent = copilot_sdk.captured["send_calls"][0]["prompt"]
    assert "verdict" in sent and "summary" in sent  # the schema is in the prompt
    assert "ONLY" in sent  # the strict return-only-JSON instruction


async def test_run_structured_delivers_given_schema(copilot_sdk, tmp_path):
    # The generic path embeds whatever schema it's handed (e.g. dedup), not the review one.
    from aeview.schema import duplicate_groups_json_schema

    copilot_sdk.queue_turn(json.dumps({"duplicate_groups": []}))
    out = await copilot.CopilotAdapter().run_structured(
        "P", duplicate_groups_json_schema(), "gpt-5.4", tmp_path, tmp_path / "log", timeout=5.0
    )
    assert out.payload == {"duplicate_groups": []}
    assert "duplicate_groups" in copilot_sdk.captured["send_calls"][0]["prompt"]  # schema embedded
    assert copilot_sdk.captured["send_calls"][0]["timeout"] == 5.0  # forwarded to send_and_wait


# --- thinking / effort -----------------------------------------------------------------


async def test_thinking_maps_to_reasoning_effort(copilot_sdk, tmp_path):
    await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log", "high")
    assert copilot_sdk.captured["create_kwargs"]["reasoning_effort"] == "high"


async def test_default_thinking_leaves_effort_unset(copilot_sdk, tmp_path):
    await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log", "default")
    assert copilot_sdk.captured["create_kwargs"]["reasoning_effort"] is None


async def test_rejects_invalid_thinking(copilot_sdk, tmp_path):
    # The SDK's effort set is low/medium/high/xhigh (no none/max); an unknown value fails fast.
    with pytest.raises(AdapterError, match="thinking"):
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log", "ultra")
    assert copilot_sdk.captured["clients"] == 0  # fails before any SDK work


# --- usage (assistant.usage events) ----------------------------------------------------


async def test_tokens_accumulate_across_reprompt(copilot_sdk, tmp_path):
    copilot_sdk.queue_turn("no json", usage=(0, 8))  # attempt 1 invalid, still billed
    copilot_sdk.queue_turn(json.dumps(_VALID), usage=(7, 12))  # attempt 2 valid
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.usage.output_tokens == 20  # both attempts counted, not just the winner
    assert out.usage.input_tokens == 7


async def test_tokens_sum_multiple_usage_events_in_one_turn(copilot_sdk, tmp_path):
    # A turn can emit several assistant.usage events (one per model call); they must accumulate.
    copilot_sdk.queue_turn(json.dumps(_VALID), usage=[(3, 4), (0, 8)])
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.usage.input_tokens == 3
    assert out.usage.output_tokens == 12


async def test_no_usage_events_yields_zero(copilot_sdk, tmp_path):
    copilot_sdk.queue_turn(json.dumps(_VALID), usage=[])  # no assistant.usage events arrived
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.usage.input_tokens == 0
    assert out.usage.output_tokens == 0
    assert out.usage.cost_usd == 0.0


# --- binary override -------------------------------------------------------------------


async def test_binary_override_threads_to_stdio_connection(copilot_sdk, tmp_path, monkeypatch):
    # settings.harnessBinaries["copilot"] reaches RuntimeConnection.for_stdio(path=...). An absolute
    # path which can't resolve is passed verbatim (so the SDK fails loud, not silently bundled).
    monkeypatch.setattr(copilot, "which", lambda b: None)
    recorded: dict = {}

    def fake_for_stdio(*, path=None, args=()):
        recorded["path"] = path
        return "CONN-SENTINEL"

    monkeypatch.setattr(copilot.RuntimeConnection, "for_stdio", staticmethod(fake_for_stdio))
    await copilot.CopilotAdapter("/custom/copilot").run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert recorded["path"] == "/custom/copilot"
    assert copilot_sdk.captured["client_kwargs"]["connection"] == "CONN-SENTINEL"


async def test_bare_name_override_resolves_via_which(copilot_sdk, tmp_path, monkeypatch):
    monkeypatch.setattr(
        copilot, "which", lambda b: "/resolved/copilot" if b == "mycopilot" else None
    )
    recorded: dict = {}
    monkeypatch.setattr(
        copilot.RuntimeConnection,
        "for_stdio",
        staticmethod(lambda *, path=None, args=(): recorded.setdefault("path", path)),
    )
    await copilot.CopilotAdapter("mycopilot").run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert recorded["path"] == "/resolved/copilot"


async def test_no_override_uses_bundled(copilot_sdk, tmp_path):
    await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert copilot_sdk.captured["client_kwargs"]["connection"] is None  # None → bundled binary


def test_empty_override_is_treated_as_bundled():
    assert copilot.CopilotAdapter("")._copilot_bin is None


# --- timeout / teardown / isolation / cancellation -------------------------------------


async def test_forwards_timeout_to_send_and_wait(copilot_sdk, tmp_path):
    await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log", None, 90.0)
    assert copilot_sdk.captured["send_calls"][0]["timeout"] == 90.0


async def test_unset_timeout_passes_unbounded_to_send_and_wait(copilot_sdk, tmp_path):
    # send_and_wait defaults to 60s; with no per-review timeout we must override it so a long review
    # isn't cut off at 60s — pass the effectively-unbounded sentinel.
    await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log", None, None)
    assert copilot_sdk.captured["send_calls"][0]["timeout"] == copilot._UNBOUNDED_TIMEOUT


async def test_timeout_is_non_transient_and_tears_down(copilot_sdk, tmp_path):
    # A per-review timeout is fail-fast AND must tear down the client: send_and_wait's own timeout
    # doesn't kill the subprocess, so the async-with stop() is what does (here: the captured count).
    copilot_sdk.queue_turn(json.dumps(_VALID), delay=10.0)
    with pytest.raises(AdapterError) as ei:
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log", None, 0.01)
    assert ei.value.transient is False
    assert "timed out" in str(ei.value)
    assert copilot_sdk.captured["stopped"] == 1  # client torn down despite the timeout


async def test_sdk_runs_off_the_caller_thread(copilot_sdk, tmp_path):
    # The SDK interaction runs on a dedicated daemon thread + its own event loop; that isolation is
    # what keeps teardown from being starved (no deadlock). Pin it so a revert to an inline await on
    # the caller's loop fails here.
    await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert copilot_sdk.captured["thread_name"] != threading.current_thread().name
    assert copilot_sdk.captured["thread_name"] == "aeview-copilot-turn"


async def test_cancellation_does_not_error_in_worker_thread(copilot_sdk, tmp_path, monkeypatch):
    # Cancelling a review mid-run must not raise InvalidStateError in the daemon thread: the future
    # is claimed (set_running_or_notify_cancel) before set_result, so the cancel can't race it.
    thread_errors: list = []
    monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(args.exc_value))
    copilot_sdk.queue_turn(json.dumps(_VALID), delay=0.3)  # keep the turn in flight across cancel
    task = asyncio.create_task(
        copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    )
    await asyncio.sleep(0.05)  # let the daemon thread start and claim the future
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)  # let the daemon thread finish + set the result on the running future
    assert thread_errors == []  # no InvalidStateError escaped the worker thread
    assert copilot_sdk.captured["stopped"] == 1  # teardown still ran


async def test_thread_start_failure_is_transient_adapter_error(copilot_sdk, tmp_path, monkeypatch):
    # OS thread-limit exhaustion: Thread.start() raising RuntimeError must normalize to a transient
    # AdapterError, not escape raw (the dedup path catches only AdapterError).
    class _BoomThread:
        def __init__(self, *args, **kwargs) -> None: ...

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(copilot.threading, "Thread", _BoomThread)
    with pytest.raises(AdapterError) as ei:
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert ei.value.transient is True


# --- error classification --------------------------------------------------------------


async def test_transient_error_by_text_retries(copilot_sdk, tmp_path):
    # The SDK has no public retryable helper, so a transient-looking error still classifies as
    # transient (restores the old CLI text classification).
    copilot_sdk.queue_turn(None, exc=RuntimeError("the server is overloaded, try again"))
    with pytest.raises(AdapterError) as ei:
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert ei.value.transient is True
    assert copilot_sdk.captured["stopped"] == 1  # torn down on the error path too


async def test_hard_error_fails_fast(copilot_sdk, tmp_path):
    copilot_sdk.queue_turn(None, exc=RuntimeError("invalid request: bad model"))
    with pytest.raises(AdapterError) as ei:
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert ei.value.transient is False


async def test_client_construction_failure_is_adapter_error(copilot_sdk, tmp_path):
    # The real SDK raises RuntimeError in CopilotClient.__init__ when the CLI can't be found; the
    # catch-all must normalize it to a (non-transient) AdapterError, not let it escape.
    copilot_sdk.set_client_init_exc(RuntimeError("Copilot CLI not found."))
    with pytest.raises(AdapterError) as ei:
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert ei.value.transient is False
    assert copilot_sdk.captured["stopped"] == 0  # never entered the context


async def test_bad_binary_override_fails_fast(tmp_path):
    # No fixture: exercise the REAL SDK. A nonexistent override → spawn fails → AdapterError, fast.
    with pytest.raises(AdapterError):
        await copilot.CopilotAdapter("/nonexistent/copilot/binary").run(
            "p", "gpt-5.4", tmp_path, tmp_path / "log", None, 30.0
        )


# --- retry-then-fail (the schema_support="prompt" reaction) ----------------------------


async def test_invalid_output_reprompts_then_fails(copilot_sdk, tmp_path):
    copilot_sdk.queue_turn("sorry, I can't produce JSON")  # attempt 1: no JSON
    copilot_sdk.queue_turn("still no json here")  # attempt 2: still bad
    with pytest.raises(AdapterError, match="did not return a JSON object"):
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    calls = copilot_sdk.captured["send_calls"]
    assert len(calls) == 2  # one re-prompt, then fail
    # attempt 2 is a follow-up turn on the SAME session: just the corrective suffix, not the prompt
    assert calls[1]["prompt"] == copilot._RETRY_SUFFIX
    assert calls[0]["prompt"] != copilot._RETRY_SUFFIX  # attempt 1 is the full embedded prompt


async def test_reprompt_recovers_on_second_attempt(copilot_sdk, tmp_path):
    copilot_sdk.queue_turn("oops no json")  # attempt 1 bad
    copilot_sdk.queue_turn(json.dumps(_VALID))  # attempt 2 good
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 2


def _bad_enum() -> str:
    # Parseable JSON with the required keys, but verdict isn't a valid enum value.
    return json.dumps({"verdict": "maybe", "summary": "x", "findings": [], "next_steps": []})


async def test_schema_invalid_enum_reprompts_then_fails(copilot_sdk, tmp_path):
    # A structurally-present but enum-invalid payload must RE-PROMPT (not slip past to fail at the
    # caller). Two bad attempts -> AdapterError after the re-prompt.
    copilot_sdk.queue_turn(_bad_enum())
    copilot_sdk.queue_turn(_bad_enum())
    with pytest.raises(AdapterError, match="schema validation"):
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert len(copilot_sdk.captured["send_calls"]) == 2


async def test_schema_invalid_enum_recovers_on_reprompt(copilot_sdk, tmp_path):
    copilot_sdk.queue_turn(_bad_enum(), usage=(0, 8))  # attempt 1: invalid enum
    copilot_sdk.queue_turn(json.dumps(_VALID), usage=(7, 12))  # attempt 2: valid
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 2
    assert out.usage.output_tokens == 20  # both attempts counted on the validate-fail re-prompt


# --- JSON extraction (feeds the answer text from a single turn) ------------------------


async def test_extracts_bare_json(copilot_sdk, tmp_path):
    copilot_sdk.queue_turn(json.dumps(_VALID))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"


async def test_extracts_fenced_json(copilot_sdk, tmp_path):
    copilot_sdk.queue_turn(f"Here is the review:\n```json\n{json.dumps(_VALID)}\n```")
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"


async def test_extracts_prose_wrapped_json_with_braces_in_strings(copilot_sdk, tmp_path):
    # Prose around the object (no fence); raw_decode parses from the `{` and ignores trailing text.
    # The JSON string fields contain { and } (code snippets), which the real parser handles.
    review = {
        "verdict": "needs-attention",
        "summary": "found one issue",
        "findings": [
            {
                "title": "guard clause",
                "body": "code reads: if (a) { return {}; } else { throw; }",
                "severity": "high",
                "category": "bug",
                "confidence": 0.8,
                "location": {"file": "a.py", "line_start": 1, "line_end": 2},
                "recommendation": "wrap the `{ ... }` block in a try",
            }
        ],
        "next_steps": [],
    }
    content = f"Sure — here is the review:\n{json.dumps(review)}\nLet me know if you need more."
    copilot_sdk.queue_turn(content)
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "needs-attention"
    assert out.review.findings[0].title == "guard clause"


async def test_extracts_prose_wrapped_json_with_escaped_quotes(copilot_sdk, tmp_path):
    # A finding string contains an escaped quote and a brace, and the preamble has an unbalanced
    # quote — neither must break extraction (raw_decode starts at the `{`).
    review = {
        "verdict": "needs-attention",
        "summary": "one issue",
        "findings": [
            {
                "title": "quote handling",
                "body": 'it printed "}" then stopped',
                "severity": "medium",
                "category": "bug",
                "confidence": 0.6,
                "location": {"file": "a.py", "line_start": 1, "line_end": 1},
                "recommendation": "escape the quote",
            }
        ],
        "next_steps": [],
    }
    copilot_sdk.queue_turn(f'Here is the "fix": {json.dumps(review)}')
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "needs-attention"
    assert out.review.findings[0].body == 'it printed "}" then stopped'


async def test_extracts_object_after_unbalanced_prose_quote(copilot_sdk, tmp_path):
    # An ODD number of quotes in the preamble must not break extraction: raw_decode starts at the
    # `{` so the preceding quote is irrelevant (it ignores leading text).
    copilot_sdk.queue_turn(f'He said "look here: {json.dumps(_VALID)}')
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"


async def test_extracts_object_after_quoted_brace_in_prose(copilot_sdk, tmp_path):
    # A quoted brace in the preamble (`a "{" b ...`) must not be taken as the object start.
    copilot_sdk.queue_turn(f'a "{{" b {json.dumps(_VALID)}')
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"


async def test_truncated_object_reprompts_and_recovers(copilot_sdk, tmp_path):
    # A token-limit cutoff yields an unclosed object: raw_decode raises (no object), extraction
    # yields None, and the adapter re-prompts (never silently accepts a partial).
    copilot_sdk.queue_turn(
        '{"verdict": "approve", "summary": "cut off here'
    )  # attempt 1: truncated
    copilot_sdk.queue_turn(json.dumps(_VALID))  # attempt 2: complete
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 2


async def test_skips_non_matching_object_whose_interior_would_exhaust_the_start_cap(
    copilot_sdk, tmp_path, monkeypatch
):
    # Guards the `i = end` advance via its observable effect: with a tiny start cap, a decoy whose
    # interior braces DON'T collapse under one decode would burn the cap if rescanned (i = start+1),
    # missing the answer. Advancing past it (i = end) reaches the answer.
    monkeypatch.setattr(copilot, "_MAX_JSON_STARTS", 3)
    decoy = {"note": "{" * 10}  # 10 in-string braces, > the cap of 3
    copilot_sdk.queue_turn(f"{json.dumps(decoy)} then: {json.dumps(_VALID)}")
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 1  # decoy=1 start, answer=2nd — within cap 3


async def test_finds_answer_after_moderate_brace_preamble(copilot_sdk, tmp_path):
    copilot_sdk.queue_turn(("{" * 100) + " " + json.dumps(_VALID))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 1


async def test_start_cap_bounds_scan_then_reprompts(copilot_sdk, tmp_path, monkeypatch):
    # The scan is bounded: a brace-heavy preamble exceeding the start cap makes attempt 1 yield no
    # match (rather than scanning unboundedly), and the adapter re-prompts.
    monkeypatch.setattr(copilot, "_MAX_JSON_STARTS", 8)
    copilot_sdk.queue_turn(("{ " * 50) + json.dumps(_VALID))  # >8 junk starts before the answer
    copilot_sdk.queue_turn(json.dumps(_VALID))  # attempt 2: clean
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 2


async def test_wrapped_answer_is_recovered_by_descent(copilot_sdk, tmp_path):
    # A prompt-only model may wrap the answer ({"output": {<review>}}); when no top-level object
    # matches, we descend and recover it — in one attempt, no re-prompt.
    copilot_sdk.queue_turn(json.dumps({"output": dict(_VALID)}))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 1


async def test_descent_does_not_match_findings_subobjects(copilot_sdk, tmp_path):
    # Descent must find the wrapped review, not a finding sub-object (findings lack verdict/summary,
    # so they never match) — guards against grabbing the wrong nested dict.
    review = dict(
        _VALID,
        verdict="needs-attention",
        findings=[
            {
                "title": "t",
                "body": "b",
                "severity": "high",
                "category": "bug",
                "confidence": 0.5,
                "location": {"file": "a.py", "line_start": 1, "line_end": 1},
                "recommendation": "r",
            }
        ],
    )
    copilot_sdk.queue_turn(json.dumps({"result": review}))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "needs-attention"
    assert out.review.findings[0].title == "t"


async def test_deeply_nested_unterminated_input_reprompts(copilot_sdk, tmp_path):
    # A deeply nested, never-closed prefix is a JSONDecodeError (the C scanner reports the
    # unterminated end, not RecursionError); it must be caught so the review re-prompts.
    copilot_sdk.queue_turn('{"a":' * 6000)  # attempt 1: unparseable
    copilot_sdk.queue_turn(json.dumps(_VALID))  # attempt 2: clean
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 2


async def test_closed_deeply_nested_object_does_not_crash_descent(copilot_sdk, tmp_path):
    # A CLOSED 2000-deep object parses fine, doesn't match, and the nested-descent fallback must
    # traverse it WITHOUT a RecursionError (iterative, not recursive), then re-prompt.
    copilot_sdk.queue_turn('{"a":' * 2000 + "1" + "}" * 2000)  # deep, no verdict/summary
    copilot_sdk.queue_turn(json.dumps(_VALID))  # attempt 2: clean
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 2


async def test_two_non_matching_objects_before_answer(copilot_sdk, tmp_path):
    # Two non-matching objects precede the answer; the SECOND is at a non-zero offset, so the cursor
    # re-base (i = start + length) must be correct or the scan re-finds it and never reaches the
    # answer.
    copilot_sdk.queue_turn(f'{{"x": 1}} {{"y": 2}} {json.dumps(_VALID)}')
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 1


async def test_object_larger_than_window_is_truncated_then_reprompts(
    copilot_sdk, tmp_path, monkeypatch
):
    # The window bounds EVERY decode (incl. the first): an object larger than the window is sliced
    # mid-object, fails to parse, and re-prompts rather than scanning the whole output.
    monkeypatch.setattr(copilot, "_MAX_SCAN_CHARS", 200)  # > bare _VALID, < the big object
    copilot_sdk.queue_turn(json.dumps(dict(_VALID, summary="x" * 500)))  # > window
    copilot_sdk.queue_turn(json.dumps(_VALID))  # attempt 2: small enough to parse
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 2


async def test_complete_large_object_within_window_is_parsed(copilot_sdk, tmp_path):
    # The window is far larger than any real review, so a verbose-but-complete answer parses in one
    # attempt (the bound only trips on pathological output, not large legitimate reviews).
    copilot_sdk.queue_turn(json.dumps(dict(_VALID, summary="x" * 50_000)))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.summary == "x" * 50_000
    assert len(copilot_sdk.captured["send_calls"]) == 1


async def test_zero_required_schema_ignores_stray_empty_object(copilot_sdk, tmp_path):
    # DuplicateGroups has no required keys; a stray {} before the real answer must NOT be accepted
    # as an empty "no duplicates" (which would silently drop dedup decisions).
    from aeview.schema import duplicate_groups_json_schema

    groups = {"duplicate_groups": [{"survivor": "f1", "duplicates": ["f2"]}]}
    copilot_sdk.queue_turn(f"{{}} {json.dumps(groups)}")
    out = await copilot.CopilotAdapter().run_structured(
        "P", duplicate_groups_json_schema(), "gpt-5.4", tmp_path, tmp_path / "log"
    )
    assert out.payload == groups  # the real object, not the stray {}


# --- direct helper unit guards ---------------------------------------------------------


def test_json_objects_does_not_raise_on_deep_nesting():
    assert list(copilot._json_objects('{"a":' * 6000)) == []


def test_find_nested_match_is_iterative_on_deep_input():
    deep: dict = {"x": 1}
    for _ in range(3000):
        deep = {"a": deep}
    assert copilot._find_nested_match(deep, {"verdict", "summary"}, set()) is None


def test_matches_zero_required_accepts_partial_not_just_full():
    props = {"a", "b"}
    assert copilot._matches({"a": 1}, set(), props) is True  # one of two -> accepted
    assert copilot._matches({"a": 1, "b": 2}, set(), props) is True
    assert copilot._matches({}, set(), props) is False  # stray empty -> rejected
    assert copilot._matches({"c": 3}, set(), props) is False  # unrelated keys -> rejected


# --- logging ---------------------------------------------------------------------------


async def test_writes_success_log(copilot_sdk, tmp_path):
    log = tmp_path / "review.log"
    await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, log)
    text = log.read_text()
    assert "--- result ---" in text
    assert '"input_tokens": 100' in text  # token accounting is logged
    assert '"output_tokens": 20' in text
    assert "approve" in text  # the answer text (review JSON) is logged


async def test_writes_error_log_on_failure(copilot_sdk, tmp_path):
    copilot_sdk.queue_turn(None, exc=RuntimeError("boom"))
    log = tmp_path / "review.log"
    with pytest.raises(AdapterError):
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, log)
    assert "--- error ---" in log.read_text()


async def test_log_write_failure_suppressed_on_success(copilot_sdk, tmp_path):
    # A failed log write (here: log_path is a directory -> OSError) is suppressed so it can't break
    # a successful review.
    logdir = tmp_path / "logdir"
    logdir.mkdir()
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, logdir)
    assert out.review.verdict == "approve"


async def test_log_write_failure_suppressed_on_error(copilot_sdk, tmp_path):
    logdir = tmp_path / "logdir"
    logdir.mkdir()
    copilot_sdk.queue_turn(None, exc=RuntimeError("boom"))
    with pytest.raises(AdapterError):
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, logdir)


# --- SDK signature pin -----------------------------------------------------------------


def test_sdk_call_kwargs_match_the_real_sdk():
    # The SDK boundary is faked elsewhere; pin the kwarg names the adapter passes against the REAL
    # SDK so an upgrade that renames one fails here rather than at runtime.
    import inspect

    from copilot import CopilotClient, CopilotSession

    init = inspect.signature(CopilotClient.__init__).parameters
    for kw in ("working_directory", "connection"):
        assert kw in init, f"CopilotClient.__init__ no longer accepts {kw}"
    create = inspect.signature(CopilotClient.create_session).parameters
    for kw in ("model", "reasoning_effort", "on_permission_request", "working_directory"):
        assert kw in create, f"CopilotClient.create_session no longer accepts {kw}"
    send = inspect.signature(CopilotSession.send_and_wait).parameters
    assert "timeout" in send, "CopilotSession.send_and_wait no longer accepts timeout"


# --- preflight -------------------------------------------------------------------------


def test_preflight_no_override_resolves_bundled_and_warns(monkeypatch):
    # No override + no COPILOT_CLI_PATH → resolve the bundled binary (need not be on PATH). Copilot
    # has no no-cost auth probe, so the best we can say is warn (present, auth unverifiable).
    monkeypatch.delenv("COPILOT_CLI_PATH", raising=False)
    pf = copilot.CopilotAdapter().preflight()
    assert pf.status == "warn"
    assert "auth not verifiable" in pf.detail


def test_preflight_override_resolves_and_warns(monkeypatch):
    monkeypatch.setattr(copilot, "which", lambda binary: f"/resolved{binary}")
    pf = copilot.CopilotAdapter("/opt/copilot").preflight()
    assert pf.status == "warn"
    assert "/resolved/opt/copilot" in pf.detail  # the override path is what resolved


def test_preflight_non_executable_override_fails(monkeypatch):
    monkeypatch.setattr(copilot, "which", lambda binary: None)  # override doesn't resolve
    pf = copilot.CopilotAdapter("/nope").preflight()
    assert pf.status == "fail"


def test_preflight_fails_when_bundle_missing(monkeypatch):
    # With no override + no COPILOT_CLI_PATH, copilot resolves ONLY its bundled binary — if that
    # path doesn't exist, doctor must FAIL (matching what the run path would do), not silently pass.
    import copilot.client as cc

    monkeypatch.delenv("COPILOT_CLI_PATH", raising=False)
    monkeypatch.setattr(cc, "_get_bundled_cli_path", lambda: "/no/bundle/copilot")
    pf = copilot.CopilotAdapter().preflight()
    assert pf.status == "fail"


def test_preflight_fails_when_bundle_path_is_none(monkeypatch):
    # _get_bundled_cli_path returns None on an unsupported/binary-less wheel — doctor FAILS.
    import copilot.client as cc

    monkeypatch.delenv("COPILOT_CLI_PATH", raising=False)
    monkeypatch.setattr(cc, "_get_bundled_cli_path", lambda: None)
    pf = copilot.CopilotAdapter().preflight()
    assert pf.status == "fail"


def test_preflight_fails_when_bundle_resolution_raises(monkeypatch):
    # If the bundled path can't be resolved at all (lookup raises), doctor FAILS rather than
    # crashing — exercises the except branch in _resolve_copilot_bin.
    import copilot.client as cc

    def boom():
        raise RuntimeError("resolution exploded")

    monkeypatch.delenv("COPILOT_CLI_PATH", raising=False)
    monkeypatch.setattr(cc, "_get_bundled_cli_path", boom)
    pf = copilot.CopilotAdapter().preflight()
    assert pf.status == "fail"


# --- dogfood r1: teardown robustness, None-answer, retry timeout, usage guard, resolution ---


async def test_raising_stop_does_not_break_the_result(copilot_sdk, tmp_path):
    # A stop() that raises must not mask a valid parsed result; _teardown_client falls back to
    # force_stop() (the SDK's escape for a stuck/failing stop()).
    copilot_sdk.set_stop_error(RuntimeError("destroy RPC failed"))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"  # the result survives a raising teardown
    assert copilot_sdk.captured["force_stopped"] == 1  # hard-kill fallback ran


async def test_none_answer_reprompts_and_recovers(copilot_sdk, tmp_path):
    # send_and_wait returns None when the model produced no assistant message — treat it as no
    # answer and re-prompt (a realistic production path: the model declines / an empty turn).
    copilot_sdk.queue_turn(None)  # attempt 1: no message
    copilot_sdk.queue_turn(json.dumps(_VALID))  # attempt 2: valid
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(copilot_sdk.captured["send_calls"]) == 2


async def test_none_answer_both_attempts_fails(copilot_sdk, tmp_path):
    copilot_sdk.queue_turn(None)
    copilot_sdk.queue_turn(None)
    with pytest.raises(AdapterError, match="did not return a JSON object"):
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")


async def test_retry_turn_also_gets_the_configured_timeout(copilot_sdk, tmp_path):
    # Both the first turn AND the same-session retry must carry the per-review timeout, not the
    # SDK's 60s default.
    copilot_sdk.queue_turn("no json")  # forces a re-prompt
    copilot_sdk.queue_turn(json.dumps(_VALID))
    await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log", None, 77.0)
    assert [c["timeout"] for c in copilot_sdk.captured["send_calls"]] == [77.0, 77.0]


async def test_none_input_tokens_counted_as_zero(copilot_sdk, tmp_path):
    # copilot can omit input tokens (AssistantUsageData.input_tokens=None); the `or 0` guard keeps
    # the run working and counts it as 0.
    copilot_sdk.queue_turn(json.dumps(_VALID), usage=(None, 9))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.usage.input_tokens == 0
    assert out.usage.output_tokens == 9


def test_preflight_honors_copilot_cli_path(monkeypatch, tmp_path):
    # No aeview override: the SDK resolves COPILOT_CLI_PATH before the bundle, so preflight must too
    # (else doctor would FAIL a run that would succeed). A valid env path → warn (resolved).
    import copilot.client as cc

    monkeypatch.setattr(cc, "_get_bundled_cli_path", lambda: None)  # no bundle
    binp = tmp_path / "copilot"
    binp.write_text("#!/bin/sh\n")
    monkeypatch.setenv("COPILOT_CLI_PATH", str(binp))
    pf = copilot.CopilotAdapter().preflight()
    assert pf.status == "warn"
    assert str(binp) in pf.detail


def test_preflight_copilot_cli_path_missing_file_fails(monkeypatch):
    import copilot.client as cc

    monkeypatch.setattr(cc, "_get_bundled_cli_path", lambda: None)
    monkeypatch.setenv("COPILOT_CLI_PATH", "/nonexistent/copilot")
    assert copilot.CopilotAdapter().preflight().status == "fail"


def test_preflight_no_override_does_not_fall_back_to_path(monkeypatch):
    # No override + no bundle + no COPILOT_CLI_PATH → fail, even if a copilot is on PATH: the SDK
    # has no PATH search for the bare binary, so doctor must not either (else it'd report OK on a
    # run that would fail). _resolve_copilot_bin's no-override path never calls which.
    import copilot.client as cc

    monkeypatch.setattr(cc, "_get_bundled_cli_path", lambda: None)
    monkeypatch.delenv("COPILOT_CLI_PATH", raising=False)
    monkeypatch.setattr(copilot, "which", lambda b: "/usr/bin/copilot")  # a PATH copilot exists
    assert copilot.CopilotAdapter().preflight().status == "fail"


def test_bundled_cli_path_symbol_exists_in_real_sdk():
    # Doctor's no-override resolution imports the private copilot.client._get_bundled_cli_path (no
    # public equivalent); pin it against the REAL SDK so an upgrade that removes it fails loudly
    # here rather than silently degrading doctor's copilot check.
    import copilot.client as cc

    assert callable(cc._get_bundled_cli_path)


# --- registry + capability -------------------------------------------------------------


def test_copilot_resolves_through_the_registry():
    adapter = get_adapter("copilot")
    assert isinstance(adapter, copilot.CopilotAdapter)
    assert adapter.schema_support == "prompt"
    assert adapter.binary == "copilot"
    assert adapter.auth_status_args == []  # no no-cost auth probe


def test_get_adapter_forwards_binary_override():
    adapter = get_adapter("copilot", "/x/copilot")
    assert isinstance(adapter, copilot.CopilotAdapter)
    assert adapter._copilot_bin == "/x/copilot"
    assert adapter.binary == "/x/copilot"
