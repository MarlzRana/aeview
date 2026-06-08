from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeview.harness import codex
from aeview.harness.base import AdapterError
from aeview.process import ProcResult

_USAGE_JSONL = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "x"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}}
        ),
    ]
)

_VALID_FINAL = json.dumps(
    {"verdict": "approve", "summary": "ok", "findings": [], "next_steps": []}
)


def _find(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


@pytest.fixture
def capture_codex(monkeypatch):
    """Replace run_async: capture argv and write the canned final message to the output file."""
    captured: dict = {}

    async def fake(args, cwd=None, log_path=None, input_text=None, timeout=None):
        captured["args"] = args
        captured["input_text"] = input_text
        captured["timeout"] = timeout
        # The schema file lives in a tempdir cleaned up after the call; capture it while it exists.
        captured["schema"] = json.loads(Path(_find(args, "--output-schema")).read_text())
        out_path = Path(_find(args, "--output-last-message"))
        out_path.write_text(_VALID_FINAL, encoding="utf-8")
        return ProcResult(0, _USAGE_JSONL, "")

    monkeypatch.setattr(codex, "run_async", fake)
    return captured


async def test_codex_runs_read_only_constrained(capture_codex, tmp_path):
    out = await codex.CodexAdapter().run("PROMPT", "gpt-5.5", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert out.usage.input_tokens == 100
    assert out.usage.output_tokens == 20
    assert out.usage.cost_usd == 0.0  # codex reports no USD cost

    args = capture_codex["args"]
    assert args[:2] == ["codex", "exec"]
    assert _find(args, "--sandbox") == "read-only"
    assert _find(args, "--model") == "gpt-5.5"
    assert '-c' in args and 'approval_policy="never"' in args
    assert "--output-schema" in args and "--output-last-message" in args
    assert "--ephemeral" in args and "--json" in args
    assert capture_codex["input_text"] == "PROMPT"  # prompt on stdin


async def test_run_structured_delivers_strict_form_of_given_schema(capture_codex, tmp_path):
    # The generic path must strictify and deliver whatever schema it's handed (e.g. dedup),
    # not the review schema — codex's constrained decoding needs every property required.
    from aeview.schema import duplicate_groups_json_schema

    await codex.CodexAdapter().run_structured(
        "P", duplicate_groups_json_schema(), "gpt-5.5", tmp_path, tmp_path / "log", timeout=5.0
    )
    schema = capture_codex["schema"]
    assert "duplicate_groups" in schema["properties"]
    assert schema["additionalProperties"] is False  # strictified
    assert "duplicate_groups" in schema["required"]
    assert capture_codex["timeout"] == 5.0  # timeout propagates to run_async


async def test_codex_forwards_timeout_to_run_async(capture_codex, tmp_path):
    await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log", None, 90.0)
    assert capture_codex["timeout"] == 90.0


async def test_codex_maps_thinking_to_reasoning_effort(capture_codex, tmp_path):
    await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log", "xhigh")
    assert 'model_reasoning_effort="xhigh"' in capture_codex["args"]


async def test_codex_rejects_invalid_thinking(capture_codex, tmp_path):
    with pytest.raises(AdapterError, match="thinking"):
        await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log", "ultra")


# --- _interpret / usage units (no subprocess) ---


def test_interpret_rejects_non_json_final():
    with pytest.raises(AdapterError, match="not JSON"):
        codex.CodexAdapter()._interpret("not json", "", "", 0)


async def test_run_rejects_schema_invalid_final(monkeypatch, tmp_path):
    # _interpret now only parses JSON; the review-shape validation lives in run().
    bad = json.dumps({"verdict": "maybe", "summary": "x", "findings": [], "next_steps": []})

    async def fake(args, cwd=None, log_path=None, input_text=None, timeout=None):
        Path(_find(args, "--output-last-message")).write_text(bad, encoding="utf-8")
        return ProcResult(0, "", "")

    monkeypatch.setattr(codex, "run_async", fake)
    with pytest.raises(AdapterError, match="schema validation"):
        await codex.CodexAdapter().run("p", "gpt-5.5", tmp_path, tmp_path / "log")


def test_interpret_nonzero_with_empty_final_is_error():
    with pytest.raises(AdapterError, match="codex exited"):
        codex.CodexAdapter()._interpret("", "", "boom", 1)


def test_interpret_nonzero_fails_even_with_a_final_message():
    # A non-zero exit must not be trusted, even if codex left a parseable final message.
    with pytest.raises(AdapterError, match="codex exited"):
        codex.CodexAdapter()._interpret(_VALID_FINAL, "", "boom", 1)


def test_interpret_timeout_is_non_transient():
    # A per-review timeout (exit 124) is fail-fast even though "timed out" reads transient to the
    # text classifier — pins codex's switch to classify_transient (not looks_transient).
    with pytest.raises(AdapterError) as ei:
        codex.CodexAdapter()._interpret("", "", "codex: timed out after 1s", 124)
    assert ei.value.transient is False


def test_interpret_error_detail_comes_from_jsonl():
    jsonl = json.dumps({"type": "turn.failed", "error": {"message": "rate limit hit"}})
    with pytest.raises(AdapterError) as ei:
        codex.CodexAdapter()._interpret("", jsonl, "Reading prompt from stdin...", 1)
    assert "rate limit hit" in str(ei.value)
    assert ei.value.transient is True  # classified from the JSONL message, not noisy stderr


def test_usage_parsing_survives_null_msg():
    null_msg = json.dumps({"type": "item.completed", "msg": None})  # would crash naive indexing
    done = {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}
    usage = codex._usage_from_jsonl(f"{null_msg}\n{json.dumps(done)}")
    assert usage.input_tokens == 7 and usage.output_tokens == 3


def test_usage_parsing_handles_msg_wrapped_event():
    jsonl = json.dumps(
        {"msg": {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}}}
    )
    usage = codex._usage_from_jsonl(jsonl)
    assert usage.input_tokens == 5 and usage.output_tokens == 2


def test_error_detail_handles_msg_wrapped_event():
    # _error_detail resolves the event shape the same way _event_usage does (consistency).
    jsonl = json.dumps({"msg": {"type": "turn.failed", "error": {"message": "boom upstream"}}})
    with pytest.raises(AdapterError) as ei:
        codex.CodexAdapter()._interpret("", jsonl, "", 1)
    assert "boom upstream" in str(ei.value)


def test_interpret_classifies_transient_text():
    with pytest.raises(AdapterError) as ei:
        codex.CodexAdapter()._interpret("", "", "rate limit exceeded", 1)
    assert ei.value.transient is True


def test_usage_from_jsonl_sums_turn_completed():
    usage = codex._usage_from_jsonl(_USAGE_JSONL)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20


def test_codex_resolves_through_the_registry():
    from aeview.harness import get_adapter

    adapter = get_adapter("codex")
    assert isinstance(adapter, codex.CodexAdapter)
    assert adapter.schema_support == "constrained"
    assert adapter.binary == "codex"
