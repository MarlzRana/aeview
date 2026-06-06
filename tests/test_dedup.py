from __future__ import annotations

import json

from aeview import dedup as dedup_mod
from aeview.config import HarnessInstance
from aeview.dedup import run_dedup
from aeview.harness.base import AdapterError, StructuredOutput
from aeview.runstore import RunStore, new_run_id
from aeview.schema import DedupResult, Location, PooledFinding, Usage


def _pool() -> list[PooledFinding]:
    return [
        PooledFinding(
            id=f"f{i}",
            title=t,
            body="b",
            severity="high",
            category="bug",
            confidence=0.7,
            location=Location(file="a.py", line_start=i, line_end=i),
            recommendation="fix",
        )
        for i, t in enumerate(["null deref", "possible null deref"], start=1)
    ]


def _instance() -> HarnessInstance:
    return HarnessInstance(harness="claude-code", model="opus", thinking="high")


class _StubAdapter:
    def __init__(self, payload: dict | None = None, error: AdapterError | None = None):
        self._payload = payload if payload is not None else {}
        self._error = error

    async def run_structured(
        self, prompt, schema, model, cwd, log_path, thinking=None, timeout=None
    ):
        log_path.write_text("stub dedup log", encoding="utf-8")
        if self._error is not None:
            raise self._error
        return StructuredOutput(payload=self._payload, usage=Usage(cost_usd=0.02), raw="{}")


async def test_run_dedup_ok_writes_artifacts_and_returns_groups(aeview_home, monkeypatch, tmp_path):
    payload = {"duplicate_groups": [{"survivor": "f2", "duplicates": ["f1"]}]}
    monkeypatch.setattr(dedup_mod, "get_adapter", lambda h: _StubAdapter(payload=payload))
    store = RunStore.create(new_run_id())

    outcome = await run_dedup(_pool(), _instance(), store, tmp_path, timeout=5.0)

    assert outcome.status == "ok"
    assert outcome.harness_id == "claude-code-opus-high"  # descriptor includes thinking
    assert outcome.groups[0].survivor == "f2"
    assert outcome.usage.cost_usd == 0.02

    inst_dir = store.dir / "dedup" / "claude-code-opus-high"
    assert (inst_dir / "prompt.md").exists()
    assert "null deref" in (inst_dir / "prompt.md").read_text()  # pool embedded in the prompt
    assert json.loads((inst_dir / "input.json").read_text())[0]["id"] == "f1"
    assert (inst_dir / "dedup.log").read_text() == "stub dedup log"
    result = DedupResult.model_validate_json((inst_dir / "result.json").read_text())
    assert result.status == "ok"
    assert result.groups[0].duplicates == ["f1"]


async def test_run_dedup_adapter_error_is_failed_outcome(aeview_home, monkeypatch, tmp_path):
    monkeypatch.setattr(
        dedup_mod, "get_adapter", lambda h: _StubAdapter(error=AdapterError("timed out"))
    )
    store = RunStore.create(new_run_id())

    outcome = await run_dedup(_pool(), _instance(), store, tmp_path, timeout=5.0)

    assert outcome.status == "failed"
    assert "timed out" in (outcome.reason or "")
    assert outcome.warning  # a notice the merge surfaces
    result = DedupResult.model_validate_json(
        (store.dir / "dedup" / "claude-code-opus-high" / "result.json").read_text()
    )
    assert result.status == "failed"


async def test_run_dedup_invalid_output_is_failed(aeview_home, monkeypatch, tmp_path):
    # Payload that doesn't match DuplicateGroups -> validation failure -> failed outcome.
    monkeypatch.setattr(
        dedup_mod, "get_adapter", lambda h: _StubAdapter(payload={"wrong": "shape"})
    )
    store = RunStore.create(new_run_id())
    outcome = await run_dedup(_pool(), _instance(), store, tmp_path, timeout=5.0)
    assert outcome.status == "failed"
