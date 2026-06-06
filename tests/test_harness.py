from __future__ import annotations

import json

import pytest

from aeview.harness import claude_code
from aeview.harness.base import AdapterError
from aeview.process import ProcResult

_VALID_CLAUDE_JSON = json.dumps(
    {
        "is_error": False,
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 5, "output_tokens": 3},
        "structured_output": {
            "verdict": "approve",
            "summary": "ok",
            "findings": [],
            "next_steps": [],
        },
    }
)


@pytest.fixture
def capture_run_async(monkeypatch):
    """Replace the adapter's run_async with a capturing stub; return the captured call."""
    captured: dict = {}

    async def fake(args, cwd=None, log_path=None, input_text=None):
        captured["args"] = args
        captured["cwd"] = cwd
        captured["input_text"] = input_text
        return ProcResult(0, _VALID_CLAUDE_JSON, "")

    monkeypatch.setattr(claude_code, "run_async", fake)
    return captured


async def test_adapter_pins_read_only_argv(capture_run_async, tmp_path):
    adapter = claude_code.ClaudeCodeAdapter()
    out = await adapter.run("REVIEW PROMPT", "sonnet", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"

    args = capture_run_async["args"]
    # The read-only contract: native sandbox + dontAsk + mutating tools disallowed.
    assert _flag_value(args, "--permission-mode") == "dontAsk"
    assert _flag_value(args, "--disallowedTools") == "Edit Write NotebookEdit"
    settings = json.loads(_flag_value(args, "--settings"))
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["filesystem"]["denyWrite"] == ["/"]
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["failIfUnavailable"] is True
    # Structured output + model wiring.
    assert _flag_value(args, "--output-format") == "json"
    assert _flag_value(args, "--model") == "sonnet"
    assert "--json-schema" in args


async def test_adapter_maps_thinking_to_effort(capture_run_async, tmp_path):
    adapter = claude_code.ClaudeCodeAdapter()
    await adapter.run("p", "opus", tmp_path, tmp_path / "log", "xhigh")
    args = capture_run_async["args"]
    assert _flag_value(args, "--effort") == "xhigh"


async def test_adapter_omits_effort_for_default_thinking(capture_run_async, tmp_path):
    adapter = claude_code.ClaudeCodeAdapter()
    await adapter.run("p", "opus", tmp_path, tmp_path / "log", "default")
    assert "--effort" not in capture_run_async["args"]
    await adapter.run("p", "opus", tmp_path, tmp_path / "log", None)
    assert "--effort" not in capture_run_async["args"]


async def test_adapter_passes_prompt_on_stdin_not_argv(capture_run_async, tmp_path):
    adapter = claude_code.ClaudeCodeAdapter()
    await adapter.run("REVIEW PROMPT", "sonnet", tmp_path, tmp_path / "log")
    assert capture_run_async["input_text"] == "REVIEW PROMPT"  # stdin
    assert "REVIEW PROMPT" not in capture_run_async["args"]  # not an argv element (ARG_MAX)


async def test_adapter_missing_binary_becomes_adapter_error(monkeypatch, tmp_path):
    async def missing(args, cwd=None, log_path=None, input_text=None):
        return ProcResult(127, "", "claude: command not found")

    monkeypatch.setattr(claude_code, "run_async", missing)
    adapter = claude_code.ClaudeCodeAdapter()
    with pytest.raises(AdapterError):
        await adapter.run("p", "sonnet", tmp_path, tmp_path / "log")


def test_interpret_classifies_rate_limit_as_transient():
    adapter = claude_code.ClaudeCodeAdapter()
    payload = json.dumps({"is_error": True, "api_error_status": 429, "result": "slow down"})
    with pytest.raises(AdapterError) as ei:
        adapter._interpret(payload, "", 1)
    assert ei.value.transient is True


def test_interpret_classifies_auth_error_as_non_transient():
    adapter = claude_code.ClaudeCodeAdapter()
    payload = json.dumps(
        {"is_error": True, "api_error_status": 401, "result": "could not load credentials"}
    )
    with pytest.raises(AdapterError) as ei:
        adapter._interpret(payload, "", 1)
    assert ei.value.transient is False


def test_interpret_missing_binary_is_non_transient():
    adapter = claude_code.ClaudeCodeAdapter()
    with pytest.raises(AdapterError) as ei:
        adapter._interpret("", "claude: command not found", 127)
    assert ei.value.transient is False


def _flag_value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]
