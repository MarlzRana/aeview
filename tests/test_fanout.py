from __future__ import annotations

import json

import pytest

from aeview import fanout
from aeview.harness.base import AdapterError, HarnessOutput
from aeview.runstore import RunStore, new_run_id
from aeview.schema import ReviewOutput, RosterEntry, Usage

_ENTRY = RosterEntry(
    id="default__claude-code-opus", reviewer="default", harness="claude-code", model="opus"
)


def _ok_output():
    return HarnessOutput(
        review=ReviewOutput(verdict="approve", summary="ok", findings=[], next_steps=[]),
        usage=Usage(),
        raw="{}",
    )


class _FlakyAdapter:
    """Fails `transient_failures` times transiently, then succeeds."""

    def __init__(self, transient_failures: int):
        self.remaining = transient_failures
        self.calls = 0

    async def run(self, prompt, model, cwd, log_path, thinking=None):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise AdapterError("rate limited", transient=True)
        return _ok_output()


class _AuthFailAdapter:
    def __init__(self):
        self.calls = 0

    async def run(self, prompt, model, cwd, log_path, thinking=None):
        self.calls += 1
        raise AdapterError("bad auth", transient=False)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(fanout, "_backoff_delay", lambda attempt: 0.0)


async def test_transient_failure_retries_then_succeeds(aeview_home, monkeypatch):
    adapter = _FlakyAdapter(transient_failures=2)
    monkeypatch.setattr(fanout, "get_adapter", lambda h: adapter)
    store = RunStore.create(new_run_id())

    [result] = await fanout.fan_out(store, [_ENTRY], {"default": "p"}, aeview_home)

    assert result.status == "done"
    assert adapter.calls == 3  # 2 failures + 1 success (MAX_ATTEMPTS)


async def test_transient_failure_exhausts_attempts(aeview_home, monkeypatch):
    adapter = _FlakyAdapter(transient_failures=99)
    monkeypatch.setattr(fanout, "get_adapter", lambda h: adapter)
    store = RunStore.create(new_run_id())

    [result] = await fanout.fan_out(store, [_ENTRY], {"default": "p"}, aeview_home)

    assert result.status == "failed"
    assert adapter.calls == fanout.MAX_ATTEMPTS  # capped


async def test_non_transient_fails_fast(aeview_home, monkeypatch):
    adapter = _AuthFailAdapter()
    monkeypatch.setattr(fanout, "get_adapter", lambda h: adapter)
    store = RunStore.create(new_run_id())

    [result] = await fanout.fan_out(store, [_ENTRY], {"default": "p"}, aeview_home)

    assert result.status == "failed"
    assert adapter.calls == 1  # no retry on non-transient


async def test_unknown_harness_marks_failed(aeview_home, monkeypatch):
    def boom(harness):
        raise AdapterError(f"harness '{harness}' is not supported")

    monkeypatch.setattr(fanout, "get_adapter", boom)
    store = RunStore.create(new_run_id())
    [result] = await fanout.fan_out(store, [_ENTRY], {"default": "p"}, aeview_home)
    assert result.status == "failed"
    assert "not supported" in (result.error or "")


async def test_failed_review_persisted_to_disk(aeview_home, monkeypatch):
    monkeypatch.setattr(fanout, "get_adapter", lambda h: _AuthFailAdapter())
    store = RunStore.create(new_run_id())
    await fanout.fan_out(store, [_ENTRY], {"default": "p"}, aeview_home)
    on_disk = json.loads(store.review_path(_ENTRY.reviewer, _ENTRY.id).read_text())
    assert on_disk["status"] == "failed"
    assert "bad auth" in on_disk["error"]
