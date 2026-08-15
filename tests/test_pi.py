from __future__ import annotations

import asyncio
import json

import pytest

from aeview.config import PiHarnessSettings, Settings
from aeview.harness import get_adapter
from aeview.harness.base import AdapterError
from aeview.harness.pi import (
    BUILTIN_ALLOWED_DOMAINS,
    PiAdapter,
    _last_assistant_text,
    _stream_error,
    _usage_from_events,
    resolve_allowed_domains,
    review_dirs,
)
from aeview.process import ProcResult

_REVIEW = {"verdict": "approve", "summary": "ok", "findings": [], "next_steps": []}


def _message_end(
    text: str, *, usage: dict | None = None, stop: str = "stop", error: str | None = None
) -> dict:
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stopReason": stop,
    }
    if usage is not None:
        message["usage"] = usage
    if error is not None:
        message["errorMessage"] = error
    return {"type": "message_end", "message": message}


class _FakeProc:
    """Minimal asyncio subprocess stand-in: stdin is a sink, stdout/stderr are byte streams."""

    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdin = _Sink()
        self.stdout = _ByteStream(stdout)
        self.stderr = _ByteStream(stderr)
        self.returncode = returncode
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


class _Sink:
    def write(self, _data: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ByteStream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._sent = False

    async def read(self, _n: int = -1) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._data


@pytest.fixture
def spawn(monkeypatch):
    """Replace create_subprocess_exec with a queue of canned processes; capture argv."""
    calls: list[dict] = []
    queue: list[_FakeProc] = []

    async def fake_exec(*argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        if not queue:
            raise AssertionError("spawn queue empty")
        return queue.pop(0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    # Don't require real binaries on PATH for run tests.
    monkeypatch.setattr("aeview.harness.pi.which", lambda name: f"/bin/{name}")
    return calls, queue


def _jsonl(*events: dict) -> bytes:
    return b"".join(json.dumps(e).encode() + b"\n" for e in events)


def _ok_proc(payload: dict | None = None, *, usage: dict | None = None) -> _FakeProc:
    body = json.dumps(payload if payload is not None else _REVIEW)
    u = usage or {"input": 10, "output": 4, "cost": {"total": 0.02}}
    return _FakeProc(_jsonl(_message_end(body, usage=u)))


# --- allowlist resolution -------------------------------------------------------------


def test_resolve_allowed_domains_default_is_builtin():
    assert resolve_allowed_domains(PiHarnessSettings()) == list(BUILTIN_ALLOWED_DOMAINS)


def test_resolve_allowed_domains_extra_unions():
    s = PiHarnessSettings(extra_sandbox_allowed_domains=["my-proxy.internal", "api.x.ai"])
    out = resolve_allowed_domains(s)
    assert out[-1] == "my-proxy.internal"
    assert out.count("api.x.ai") == 1  # already in built-in; not duplicated
    assert "api.anthropic.com" in out


def test_resolve_allowed_domains_only_replaces_and_ignores_extra():
    s = PiHarnessSettings(
        extra_sandbox_allowed_domains=["should-be-ignored.example"],
        only_sandbox_allowed_domains=["only.example"],
    )
    assert resolve_allowed_domains(s) == ["only.example"]


def test_resolve_allowed_domains_only_empty_is_no_network():
    s = PiHarnessSettings(only_sandbox_allowed_domains=[])
    assert resolve_allowed_domains(s) == []


def test_settings_harness_settings_roundtrip():
    s = Settings.model_validate(
        {
            "harnessSettings": {
                "pi": {
                    "extraSandboxAllowedDomains": ["proxy.internal"],
                    "onlySandboxAllowedDomains": None,
                },
                "claude": {"ignored": True},  # extra="ignore" on the bag
            }
        }
    )
    assert s.harness_settings.pi.extra_sandbox_allowed_domains == ["proxy.internal"]
    assert s.harness_settings.pi.only_sandbox_allowed_domains is None


def test_pi_harness_settings_rejects_unknown_key():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PiHarnessSettings.model_validate({"notAField": 1})


# --- paths / isolation ----------------------------------------------------------------


def test_review_dirs_are_siblings_of_the_log(tmp_path):
    log = tmp_path / "reviewers" / "default" / "pi-xai-grok" / "review.log"
    review_dir, session_dir, agent_dir, settings_path = review_dirs(log)
    assert review_dir == log.parent
    assert session_dir == log.parent / "pi-session"
    assert agent_dir == log.parent / "pi-agent"
    assert settings_path == log.parent / "srt-settings.json"


def test_two_reviews_get_distinct_session_dirs(tmp_path):
    a = tmp_path / "run" / "reviewers" / "default" / "pi-a" / "review.log"
    b = tmp_path / "run" / "reviewers" / "default" / "pi-b" / "review.log"
    _, sa, aa, _ = review_dirs(a)
    _, sb, ab, _ = review_dirs(b)
    assert sa != sb and aa != ab
    assert sa.parent != sb.parent


# --- spawn / argv / sandbox -----------------------------------------------------------


async def test_argv_and_srt_settings(spawn, aeview_home, tmp_path):
    calls, queue = spawn
    queue.append(_ok_proc())
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    out = await PiAdapter().run("REVIEW", "xai/grok-4.6", tmp_path, log, thinking="high")
    assert out.review.verdict == "approve"

    argv = calls[0]["argv"]
    assert argv[0] == "/bin/srt"
    assert argv[1:4] == ["--settings", str(log.parent / "srt-settings.json"), "--"]
    assert argv[4] == "/bin/pi"
    assert argv[5:8] == ["-p", "--mode", "json"]
    assert "--session-dir" in argv and argv[argv.index("--session-dir") + 1] == str(
        log.parent / "pi-session"
    )
    assert argv[argv.index("--session-id") + 1] == "aeview"
    tools = argv[argv.index("--tools") + 1]
    assert tools == "read,grep,find,ls,bash"
    assert "edit" not in tools
    assert "write" not in tools
    assert argv[argv.index("--model") + 1] == "xai/grok-4.6"
    assert argv[argv.index("--thinking") + 1] == "high"
    for flag in (
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
    ):
        assert flag in argv
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path)

    settings = json.loads((log.parent / "srt-settings.json").read_text())
    assert settings["filesystem"]["denyRead"] == []
    assert settings["filesystem"]["denyWrite"] == ["/"]
    allow = settings["filesystem"]["allowWrite"]
    assert str((log.parent / "pi-session").resolve()) in allow
    assert str((log.parent / "pi-agent").resolve()) in allow
    assert "api.x.ai" in settings["network"]["allowedDomains"]
    assert (log.parent / "pi-session").is_dir()
    assert (log.parent / "pi-agent").is_dir()
    assert calls[0]["kwargs"]["env"]["PI_CODING_AGENT_DIR"] == str(log.parent / "pi-agent")


async def test_default_thinking_omits_flag(spawn, aeview_home, tmp_path):
    calls, queue = spawn
    queue.append(_ok_proc())
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    await PiAdapter().run("p", "xai/grok-4.6", tmp_path, log, thinking="default")
    assert "--thinking" not in calls[0]["argv"]


async def test_invalid_thinking_fails_fast(tmp_path):
    with pytest.raises(AdapterError, match="thinking"):
        await PiAdapter().run("p", "xai/grok-4.6", tmp_path, tmp_path / "log", thinking="nope")


async def test_wipe_only_this_review_session(spawn, aeview_home, tmp_path):
    _, queue = spawn
    queue.append(_ok_proc())
    mine = tmp_path / "a" / "review.log"
    sibling = tmp_path / "b" / "pi-session"
    mine.parent.mkdir()
    sibling.mkdir(parents=True)
    (sibling / "keep.txt").write_text("other review")
    stale = mine.parent / "pi-session"
    stale.mkdir()
    (stale / "old.jsonl").write_text("stale")
    await PiAdapter().run("p", "xai/grok-4.6", tmp_path, mine)
    assert not (stale / "old.jsonl").exists()  # wiped
    assert (sibling / "keep.txt").read_text() == "other review"  # sibling untouched


async def test_usage_and_cost_from_message_end(spawn, aeview_home, tmp_path):
    _, queue = spawn
    queue.append(_ok_proc(usage={"input": 100, "output": 40, "cost": {"total": 0.25}}))
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    out = await PiAdapter().run("p", "xai/grok-4.6", tmp_path, log)
    assert out.usage.input_tokens == 100
    assert out.usage.output_tokens == 40
    assert out.usage.cost_usd == 0.25


async def test_binary_override_is_resolved_via_which(spawn, aeview_home, tmp_path, monkeypatch):
    calls, queue = spawn
    queue.append(_ok_proc())
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    asked: list[str] = []

    def fake_which(name: str) -> str:
        asked.append(name)
        return f"/resolved/{name}"

    monkeypatch.setattr("aeview.harness.pi.which", fake_which)
    await PiAdapter("/opt/custom-pi").run("p", "xai/grok-4.6", tmp_path, log)
    assert "/opt/custom-pi" in asked
    assert "srt" in asked
    assert calls[0]["argv"][0] == "/resolved/srt"
    assert calls[0]["argv"][4] == "/resolved//opt/custom-pi"


def test_get_adapter_forwards_override():
    adapter = get_adapter("pi", "/x/pi")
    assert adapter.binary == "/x/pi"
    assert adapter.schema_support == "prompt"


# --- re-prompt ------------------------------------------------------------------------


async def test_invalid_json_reprompts_once_same_session(spawn, aeview_home, tmp_path):
    calls, queue = spawn
    queue.append(_FakeProc(_jsonl(_message_end("not json"))))
    queue.append(_ok_proc())
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    out = await PiAdapter().run("REVIEW", "xai/grok-4.6", tmp_path, log)
    assert out.review.verdict == "approve"
    assert len(calls) == 2
    session = str(log.parent / "pi-session")
    assert calls[0]["argv"][calls[0]["argv"].index("--session-dir") + 1] == session
    assert calls[1]["argv"][calls[1]["argv"].index("--session-dir") + 1] == session
    assert (log.parent / "pi-session").is_dir()


async def test_two_invalid_answers_fail(spawn, aeview_home, tmp_path):
    _, queue = spawn
    queue.append(_FakeProc(_jsonl(_message_end("nope"))))
    queue.append(_FakeProc(_jsonl(_message_end("still nope"))))
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    with pytest.raises(AdapterError, match="matching the schema"):
        await PiAdapter().run("p", "xai/grok-4.6", tmp_path, log)


async def test_schema_invalid_payload_reprompts(spawn, aeview_home, tmp_path):
    calls, queue = spawn
    queue.append(_ok_proc({"summary": "no verdict"}))
    queue.append(_ok_proc())
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    out = await PiAdapter().run("p", "xai/grok-4.6", tmp_path, log)
    assert out.review.verdict == "approve"
    assert len(calls) == 2


# --- errors ---------------------------------------------------------------------------


async def test_nonzero_exit_is_adapter_error(spawn, aeview_home, tmp_path):
    _, queue = spawn
    queue.append(_FakeProc(b"", stderr=b"auth failed", returncode=1))
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    with pytest.raises(AdapterError, match="exited 1") as ei:
        await PiAdapter().run("p", "xai/grok-4.6", tmp_path, log)
    assert ei.value.transient is False


async def test_rate_limit_exit_is_transient(spawn, aeview_home, tmp_path):
    _, queue = spawn
    queue.append(_FakeProc(b"", stderr=b"rate limit hit, try again", returncode=1))
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    with pytest.raises(AdapterError) as ei:
        await PiAdapter().run("p", "xai/grok-4.6", tmp_path, log)
    assert ei.value.transient is True


async def test_stream_error_stop_reason(spawn, aeview_home, tmp_path):
    _, queue = spawn
    queue.append(_FakeProc(_jsonl(_message_end("x", stop="error", error="overloaded, try again"))))
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    with pytest.raises(AdapterError, match="overloaded") as ei:
        await PiAdapter().run("p", "xai/grok-4.6", tmp_path, log)
    assert ei.value.transient is True


async def test_timeout_is_fail_fast(monkeypatch, aeview_home, tmp_path):
    async def hang_exec(*_a, **_k):
        class P(_FakeProc):
            def __init__(self) -> None:
                super().__init__(b"")

            async def wait(self) -> int:
                await asyncio.sleep(10)
                return 0

        return P()

    monkeypatch.setattr("asyncio.create_subprocess_exec", hang_exec)
    monkeypatch.setattr("aeview.harness.pi.which", lambda name: f"/bin/{name}")
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    with pytest.raises(AdapterError, match="timed out") as ei:
        await PiAdapter().run("p", "xai/grok-4.6", tmp_path, log, timeout=0.01)
    assert ei.value.transient is False


async def test_missing_pi_is_non_transient(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "aeview.harness.pi.which", lambda name: None if name == "pi" else f"/bin/{name}"
    )
    with pytest.raises(AdapterError, match="pi binary not found") as ei:
        await PiAdapter().run("p", "xai/grok-4.6", tmp_path, tmp_path / "log")
    assert ei.value.transient is False


async def test_missing_srt_is_non_transient(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "aeview.harness.pi.which", lambda name: None if name == "srt" else f"/bin/{name}"
    )
    with pytest.raises(AdapterError, match="srt not found") as ei:
        await PiAdapter().run("p", "xai/grok-4.6", tmp_path, tmp_path / "log")
    assert ei.value.transient is False


# --- event helpers --------------------------------------------------------------------


def test_last_assistant_text_takes_the_last_message_end():
    events = [
        _message_end("first"),
        {"type": "agent_start"},
        _message_end("second"),
    ]
    assert _last_assistant_text(events) == "second"


def test_usage_sums_assistant_message_ends():
    events = [
        _message_end("a", usage={"input": 5, "output": 1, "cost": {"total": 0.1}}),
        _message_end("b", usage={"input": 7, "output": 2, "cost": {"total": 0.2}}),
    ]
    u = _usage_from_events(events)
    assert u.input_tokens == 12
    assert u.output_tokens == 3
    assert u.cost_usd == pytest.approx(0.3)


def test_stream_error_reads_last_assistant_stop_reason():
    assert _stream_error([_message_end("x", stop="error", error="boom")]) == "boom"
    assert _stream_error([_message_end("x")]) is None


# --- preflight ------------------------------------------------------------------------


def test_preflight_fails_when_pi_missing(monkeypatch):
    monkeypatch.setattr("aeview.harness.pi.which", lambda name: None)
    pf = PiAdapter().preflight()
    assert pf.status == "fail"
    assert "pi" in pf.detail


def test_preflight_fails_when_srt_missing(monkeypatch):
    monkeypatch.setattr(
        "aeview.harness.pi.which", lambda name: None if name == "srt" else "/bin/pi"
    )
    pf = PiAdapter().preflight()
    assert pf.status == "fail"
    assert "srt" in pf.detail


def test_preflight_warns_when_both_present(monkeypatch):
    monkeypatch.setattr("aeview.harness.pi.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("aeview.harness.pi.run_sync", lambda *a, **k: ProcResult(0, "0.84.1", ""))
    pf = PiAdapter().preflight()
    assert pf.status == "warn"
    assert "auth not verifiable" in pf.detail


async def test_writes_event_log(spawn, aeview_home, tmp_path):
    _, queue = spawn
    queue.append(_ok_proc())
    log = tmp_path / "inst" / "review.log"
    log.parent.mkdir()
    await PiAdapter().run("p", "xai/grok-4.6", tmp_path, log)
    lines = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert lines[0]["kind"] == "meta"
    assert lines[0]["event"] == {"harness": "pi", "model": "xai/grok-4.6"}
    assert any(ln["kind"] == "event" for ln in lines)
    assert lines[-1]["kind"] == "result"
