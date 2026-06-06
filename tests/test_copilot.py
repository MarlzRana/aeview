from __future__ import annotations

import json

import pytest

from aeview.harness import copilot, get_adapter
from aeview.harness.base import AdapterError
from aeview.process import ProcResult

_VALID = {"verdict": "approve", "summary": "ok", "findings": [], "next_steps": []}


def _assistant(content: str, output_tokens: int = 12) -> dict:
    return {
        "type": "assistant.message",
        "data": {"content": content, "outputTokens": output_tokens},
    }


def _stream(content: str, output_tokens: int = 12) -> str:
    """A minimal copilot JSONL stream: a session event, the assistant message, the result."""
    events = [
        {"type": "user.message", "data": {"content": "..."}},
        _assistant(content, output_tokens),
        {"type": "result", "exitCode": 0, "usage": {"premiumRequests": 1}},
    ]
    return "\n".join(json.dumps(e) for e in events)


class _FakeCopilot:
    """Records run_async calls and returns queued ProcResults in order."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._queue: list[ProcResult] = []

    def queue(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self._queue.append(ProcResult(returncode, stdout, stderr))

    async def __call__(self, args, cwd=None, log_path=None, input_text=None, timeout=None):
        self.calls.append({"args": args, "input_text": input_text, "timeout": timeout})
        return self._queue.pop(0) if self._queue else ProcResult(0, _stream(json.dumps(_VALID)), "")


@pytest.fixture
def fake_copilot(monkeypatch):
    fake = _FakeCopilot()
    monkeypatch.setattr(copilot, "run_async", fake)
    return fake


def _flag_value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


# --- argv / read-only contract ---------------------------------------------------------


async def test_read_only_blocklist_argv_and_stdin(fake_copilot, tmp_path):
    out = await copilot.CopilotAdapter().run("REVIEW", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"

    args = fake_copilot.calls[0]["args"]
    assert args[0] == "copilot"
    assert _flag_value(args, "--output-format") == "json"
    assert _flag_value(args, "--stream") == "off"
    assert "--allow-all-tools" in args
    # blocklist: write/shell/url denied (denial beats allow-all), MCP + ask_user off
    assert "--deny-tool=write" in args and "--deny-tool=shell" in args and "--deny-tool=url" in args
    assert "--disable-builtin-mcps" in args and "--no-ask-user" in args
    assert _flag_value(args, "--model") == "gpt-5.4"
    assert "REVIEW" not in args  # prompt is not an argv element
    assert "REVIEW" in fake_copilot.calls[0]["input_text"]  # it's on stdin


async def test_schema_embedded_in_prompt(fake_copilot, tmp_path):
    await copilot.CopilotAdapter().run("REVIEW", "gpt-5.4", tmp_path, tmp_path / "log")
    sent = fake_copilot.calls[0]["input_text"]
    assert "verdict" in sent and "summary" in sent  # the schema is in the prompt
    assert "ONLY" in sent  # the strict return-only-JSON instruction


async def test_thinking_maps_to_effort(fake_copilot, tmp_path):
    await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log", "high")
    assert _flag_value(fake_copilot.calls[0]["args"], "--effort") == "high"


async def test_rejects_invalid_thinking(fake_copilot, tmp_path):
    with pytest.raises(AdapterError, match="thinking"):
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log", "ultra")


# --- JSONL extraction ------------------------------------------------------------------


async def test_extracts_bare_json_from_assistant_message(fake_copilot, tmp_path):
    fake_copilot.queue(_stream(json.dumps(_VALID)))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert out.usage.output_tokens == 12  # from assistant.message.outputTokens
    assert out.usage.cost_usd == 0.0  # copilot reports no USD cost


async def test_extracts_fenced_json_from_assistant_message(fake_copilot, tmp_path):
    fenced = f"Here is the review:\n```json\n{json.dumps(_VALID)}\n```"
    fake_copilot.queue(_stream(fenced))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"


async def test_last_assistant_message_wins_and_tokens_sum(fake_copilot, tmp_path):
    # With --allow-all-tools copilot emits intermediate assistant messages while it reads, then
    # the final answer. Extraction must take the LAST message; usage sums tokens across all.
    events = [
        _assistant("Reading the files now...", output_tokens=5),
        _assistant(json.dumps(_VALID), output_tokens=12),
        {"type": "result", "exitCode": 0, "usage": {"premiumRequests": 1}},
    ]
    fake_copilot.queue("\n".join(json.dumps(e) for e in events))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"  # last message, not the "Reading..." one
    assert out.usage.output_tokens == 17  # 5 + 12 summed


async def test_tokens_accumulate_across_reprompt(fake_copilot, tmp_path):
    fake_copilot.queue(_stream("no json", output_tokens=8))  # attempt 1 (invalid) still billed
    fake_copilot.queue(_stream(json.dumps(_VALID), output_tokens=12))  # attempt 2 valid
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.usage.output_tokens == 20  # both attempts counted, not just the winner


async def test_skips_non_json_lines_in_stream(fake_copilot, tmp_path):
    # A stray banner / blank line in stdout must be tolerated, not crash extraction.
    stream = "Starting copilot...\n\n" + _stream(json.dumps(_VALID)) + "\ntrailing noise"
    fake_copilot.queue(stream)
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"


async def test_extracts_bare_top_level_json_when_no_assistant_message(fake_copilot, tmp_path):
    # Fallback: if no assistant.message carries the answer, recover a bare top-level JSON blob
    # from the raw stream (guards the stdout-scan fallback against being silently dead).
    stream = '{"type":"result","exitCode":0}\n' + json.dumps(_VALID)
    fake_copilot.queue(stream)
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"


async def test_extracts_prose_wrapped_json_with_braces_in_strings(fake_copilot, tmp_path):
    # Prose around the object (no fence) forces the brace-span fallback; the JSON string fields
    # contain { and } (code snippets), which a naive brace counter would mis-split.
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
    fake_copilot.queue(_stream(content))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "needs-attention"
    assert out.review.findings[0].title == "guard clause"


async def test_extracts_prose_wrapped_json_with_escaped_quotes_and_stray_prose_quote(
    fake_copilot, tmp_path
):
    # Prose contains an unbalanced quote (`the "fix":`) before the object, and a finding string
    # contains an escaped quote and a brace. Both must be handled: the prose quote must not flip
    # string state at depth 0, and the escaped quote inside the object must not close the string.
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
    content = f'Here is the "fix": {json.dumps(review)}'
    fake_copilot.queue(_stream(content))
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "needs-attention"
    assert out.review.findings[0].body == 'it printed "}" then stopped'


# --- retry-then-fail (the schema_support="prompt" reaction) ----------------------------


async def test_invalid_output_reprompts_then_fails(fake_copilot, tmp_path):
    fake_copilot.queue(_stream("sorry, I can't produce JSON"))  # attempt 1: no JSON
    fake_copilot.queue(_stream("still no json here"))  # attempt 2: still bad
    with pytest.raises(AdapterError, match="did not return a JSON object"):
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert len(fake_copilot.calls) == 2  # one re-prompt, then fail
    # attempt 2 carries the corrective suffix; attempt 1 does not
    assert "IMPORTANT" in fake_copilot.calls[1]["input_text"]
    assert "IMPORTANT" not in fake_copilot.calls[0]["input_text"]


async def test_reprompt_recovers_on_second_attempt(fake_copilot, tmp_path):
    fake_copilot.queue(_stream("oops no json"))  # attempt 1 bad
    fake_copilot.queue(_stream(json.dumps(_VALID)))  # attempt 2 good
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(fake_copilot.calls) == 2


# --- failure modes ---------------------------------------------------------------------


async def test_nonzero_exit_fails_fast(fake_copilot, tmp_path):
    fake_copilot.queue("", returncode=1, stderr="not authenticated")
    with pytest.raises(AdapterError, match="copilot exited 1"):
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert len(fake_copilot.calls) == 1  # hard failure -> no re-prompt


async def test_nonzero_exit_classifies_transient(fake_copilot, tmp_path):
    fake_copilot.queue("", returncode=1, stderr="rate limit exceeded, try again")
    with pytest.raises(AdapterError) as ei:
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert ei.value.transient is True


def _bad_enum() -> str:
    # Parseable JSON with the required keys, but verdict isn't a valid enum value.
    return json.dumps({"verdict": "maybe", "summary": "x", "findings": [], "next_steps": []})


async def test_schema_invalid_enum_reprompts_then_fails(fake_copilot, tmp_path):
    # A structurally-present but enum-invalid payload must RE-PROMPT (not slip past to fail at
    # the caller). Two bad attempts -> AdapterError after the re-prompt.
    fake_copilot.queue(_stream(_bad_enum()))
    fake_copilot.queue(_stream(_bad_enum()))
    with pytest.raises(AdapterError, match="schema validation"):
        await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert len(fake_copilot.calls) == 2


async def test_schema_invalid_enum_recovers_on_reprompt(fake_copilot, tmp_path):
    fake_copilot.queue(_stream(_bad_enum()))  # attempt 1: invalid enum
    fake_copilot.queue(_stream(json.dumps(_VALID)))  # attempt 2: valid
    out = await copilot.CopilotAdapter().run("p", "gpt-5.4", tmp_path, tmp_path / "log")
    assert out.review.verdict == "approve"
    assert len(fake_copilot.calls) == 2


# --- registry + capability -------------------------------------------------------------


def test_copilot_resolves_through_registry():
    adapter = get_adapter("copilot")
    assert isinstance(adapter, copilot.CopilotAdapter)
    assert adapter.schema_support == "prompt"
    assert adapter.binary == "copilot"
    assert adapter.auth_status_args == []  # no no-cost auth probe


async def test_run_structured_delivers_given_schema(fake_copilot, tmp_path):
    # The generic path embeds whatever schema it's handed (e.g. dedup), not the review one.
    from aeview.schema import duplicate_groups_json_schema

    fake_copilot.queue(_stream(json.dumps({"duplicate_groups": []})))
    out = await copilot.CopilotAdapter().run_structured(
        "P", duplicate_groups_json_schema(), "gpt-5.4", tmp_path, tmp_path / "log", timeout=5.0
    )
    assert out.payload == {"duplicate_groups": []}
    assert "duplicate_groups" in fake_copilot.calls[0]["input_text"]  # schema embedded
    assert fake_copilot.calls[0]["timeout"] == 5.0
