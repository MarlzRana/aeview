from __future__ import annotations

import pytest
from pydantic import ValidationError

from aeview.config import Retention, Settings
from aeview.runstore import (
    RunStore,
    latest_run_id,
    list_manifests,
    now_iso,
    prune_runs,
)
from aeview.schema import Invocation, RunManifest, ScopeSpec


def _write_run(run_id: str, created_at: str, *, overall: str = "done") -> None:
    store = RunStore.create(run_id)
    store.write_manifest(
        RunManifest(
            run_id=run_id,
            created_at=created_at,
            overall=overall,
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=[],
        )
    )


def _ids() -> set[str]:
    return {m.run_id for m in list_manifests()}


def test_list_manifests_newest_first(aeview_home):
    _write_run("a", "2026-06-01T10:00:00Z")
    _write_run("b", "2026-06-03T10:00:00Z")
    _write_run("c", "2026-06-02T10:00:00Z")
    assert [m.run_id for m in list_manifests()] == ["b", "c", "a"]
    assert latest_run_id() == "b"


def test_list_manifests_skips_corrupt_run(aeview_home):
    from aeview.config import runs_dir

    _write_run("good", now_iso())
    bad = runs_dir() / "corrupt"
    bad.mkdir(parents=True)
    (bad / "run.json").write_text("{not valid json")
    assert _ids() == {"good"}  # the unreadable dir is skipped, not an error


def test_latest_run_id_none_when_empty(aeview_home):
    assert latest_run_id() is None


def test_prune_keeps_newest_keep_last(aeview_home):
    # ttl far in the future so only the count cap applies.
    for i in range(5):
        _write_run(f"r{i}", f"2026-06-0{i + 1}T10:00:00Z")
    removed = prune_runs(Retention(keep_last=2, ttl_days=36500))
    assert set(removed) == {"r0", "r1", "r2"}  # newest two (r4, r3) kept
    assert _ids() == {"r3", "r4"}


def test_prune_ttl_removes_old_even_within_keep_last(aeview_home):
    _write_run("old", "2000-01-01T00:00:00Z")
    _write_run("fresh", now_iso())
    removed = prune_runs(Retention(keep_last=20, ttl_days=14))
    assert removed == ["old"]  # within the count cap but older than ttlDays
    assert _ids() == {"fresh"}


def test_prune_never_deletes_a_running_run(aeview_home):
    # keep_last=0 + tiny ttl would delete everything terminal, but a 'running' run is spared
    # (it may still be writing; I6a has no liveness check to prove otherwise).
    _write_run("live", "2000-01-01T00:00:00Z", overall="running")
    assert prune_runs(Retention(keep_last=0, ttl_days=1)) == []
    assert _ids() == {"live"}


def test_prune_deletes_enumerated_dir_not_manifest_run_id(aeview_home):
    # Security guard: prune must delete the dir it scanned, never a path rebuilt from the
    # manifest's self-declared run_id. A stale run whose run.json *lies* (run_id names another,
    # protected run) must not redirect the delete onto that other run.
    from aeview.config import runs_dir

    _write_run("keep", now_iso())  # recent -> protected by keepLast, must survive
    liar = RunStore.create("liar")
    liar.write_manifest(
        RunManifest(
            run_id="keep",  # if prune trusted this, runs_dir()/"keep" would be deleted
            created_at="2000-01-01T00:00:00Z",
            overall="done",
            invocation=Invocation(reviewers=["d"], scope=ScopeSpec(type="working-tree")),
            roster=[],
        )
    )
    prune_runs(Retention(keep_last=1, ttl_days=14))
    assert (runs_dir() / "keep").exists()  # the lie did not redirect the delete
    assert not (runs_dir() / "liar").exists()  # the enumerated stale dir was removed


def test_prune_keep_last_zero_protects_nothing(aeview_home):
    # The destructive boundary: keepLast=0 must protect *no* run, even a fresh terminal one.
    # A `runs[: keep_last or None]` regression (0 -> "unlimited") would keep it and fail here.
    _write_run("term", now_iso())  # recent + terminal
    removed = prune_runs(Retention(keep_last=0, ttl_days=36500))
    assert removed == ["term"]
    assert _ids() == set()


def test_prune_unparseable_created_at_survives_ttl(aeview_home):
    # A syntactically-valid-but-unparseable timestamp -> _parse_ts None -> never "too old", so
    # ttl can't evict it (only the count cap can). Guards the `ts is not None` branch in prune.
    _write_run("bad", "not-a-timestamp")
    removed = prune_runs(Retention(keep_last=20, ttl_days=1))  # within count, ancient ttl
    assert removed == []
    assert _ids() == {"bad"}


def test_retention_rejects_nonpositive_bounds_at_the_config_boundary():
    # The validation IS the only guard between a settings.json typo and total history loss.
    with pytest.raises(ValidationError):
        Retention(ttl_days=0)  # 0 would make cutoff==now and prune every terminal run
    with pytest.raises(ValidationError):
        Retention(keep_last=-1)
    with pytest.raises(ValidationError):
        Settings.model_validate({"retention": {"ttlDays": 0}})  # through settings.json shape
