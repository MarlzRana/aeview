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

    async def fake(args, cwd=None, log_path=None, input_text=None):
        captured["args"] = args
        captured["input_text"] = input_text
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


def test_interpret_rejects_schema_invalid_final():
    bad = json.dumps({"verdict": "maybe", "summary": "x", "findings": [], "next_steps": []})
    with pytest.raises(AdapterError, match="schema validation"):
        codex.CodexAdapter()._interpret(bad, "", "", 0)


def test_interpret_nonzero_with_empty_final_is_error():
    with pytest.raises(AdapterError, match="codex exited"):
        codex.CodexAdapter()._interpret("", "", "boom", 1)


def test_interpret_classifies_transient_text():
    with pytest.raises(AdapterError) as ei:
        codex.CodexAdapter()._interpret("", "", "rate limit exceeded", 1)
    assert ei.value.transient is True


def test_usage_from_jsonl_sums_turn_completed():
    usage = codex._usage_from_jsonl(_USAGE_JSONL)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20
