from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from aeview.config import Retention, Settings
from aeview.runstore import (
    RunBusy,
    RunStore,
    claim_run,
    effective_overall,
    latest_run_id,
    list_manifests,
    now_iso,
    pid_alive,
    prune_runs,
    reconcile_interrupted,
)
from aeview.schema import Invocation, RunManifest, ScopeSpec

# A pid that fits pid_t but is overwhelmingly unlikely to be a live process -> reads as dead.
_DEAD_PID = 999_999


def _write_run(
    run_id: str, created_at: str, *, overall: str = "done", pid: int | None = None
) -> None:
    store = RunStore.create(run_id)
    store.write_manifest(
        RunManifest(
            run_id=run_id,
            created_at=created_at,
            overall=overall,
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=[],
            pid=pid,
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


def test_identical_timestamps_order_deterministically_by_run_id(aeview_home):
    # Identical created_at -> deterministic run_id tiebreak (reverse), never filesystem order.
    # Dropping the secondary run_id key would make same-instant ordering flaky.
    ts = "2026-06-07T10:00:00.000000Z"
    for rid in ("aaa", "ccc", "bbb"):
        _write_run(rid, ts)
    assert [m.run_id for m in list_manifests()] == ["ccc", "bbb", "aaa"]
    assert latest_run_id() == "ccc"


def test_prune_keeps_newest_keep_last(aeview_home):
    # All runs are aged out (past ttl); keepLast=2 floors the newest two, the rest are pruned
    # (a run is deleted only when it's BOTH outside the floor AND older than ttlDays).
    for i in range(5):
        _write_run(f"r{i}", f"2000-01-0{i + 1}T10:00:00Z")  # ancient -> all past ttl
    removed = prune_runs(Retention(keep_last=2, ttl_days=14))
    assert set(removed) == {"r0", "r1", "r2"}  # newest two (r4, r3) floored
    assert _ids() == {"r3", "r4"}


def test_prune_keep_last_floor_protects_old_runs(aeview_home):
    # keepLast is a floor: a run within the newest keepLast is NOT evicted by ttl, even if ancient.
    _write_run("old", "2000-01-01T00:00:00Z")
    _write_run("fresh", now_iso())
    removed = prune_runs(Retention(keep_last=20, ttl_days=14))
    assert removed == []  # both within the keepLast floor -> kept despite "old" being ancient
    assert _ids() == {"old", "fresh"}


def test_now_iso_has_microsecond_precision():
    import re

    # Pins the micro-resolution stamp that makes back-to-back runs orderable; a revert to
    # second precision would silently reintroduce same-second ties in latest/prune ordering.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", now_iso())


def test_iter_skips_dir_without_run_json_and_stray_files(aeview_home):
    from aeview.config import runs_dir

    _write_run("good", now_iso())
    (runs_dir() / "no_manifest").mkdir()  # a dir but no run.json (early-crash) -> skipped
    (runs_dir() / "stray.txt").write_text("x")  # a non-dir entry -> skipped
    assert _ids() == {"good"}


def test_iter_skips_invalid_utf8_run_json(aeview_home):
    from aeview.config import runs_dir

    _write_run("good", now_iso())
    bad = runs_dir() / "badbytes"
    bad.mkdir()
    (bad / "run.json").write_bytes(b"\xff\xfe not valid utf-8")  # UnicodeDecodeError, not OSError
    assert _ids() == {"good"}  # decode error skipped, not a crash


def test_prune_delete_failure_is_skipped(aeview_home, monkeypatch):
    # Best-effort delete: an undeletable run is skipped, never aborts the caller (start of `run`).
    _write_run("old", "2000-01-01T00:00:00Z")

    def boom(_path):
        raise OSError("cannot remove")

    monkeypatch.setattr("aeview.runstore.shutil.rmtree", boom)
    assert prune_runs(Retention(keep_last=0, ttl_days=1)) == []  # delete failed -> not reported
    assert _ids() == {"old"}  # still present


def test_prune_never_deletes_a_running_run(aeview_home):
    # keep_last=0 + tiny ttl would delete everything terminal, but a 'running' run is spared
    # (it may still be writing; I6a has no liveness check to prove otherwise).
    _write_run("live", "2000-01-01T00:00:00Z", overall="running")
    assert prune_runs(Retention(keep_last=0, ttl_days=1)) == []
    assert _ids() == {"live"}


def test_prune_running_run_does_not_consume_keep_last_slot(aeview_home):
    # A newer 'running' run must not occupy a keepLast slot and evict the (older) terminal run.
    # Protection counts terminal runs only, so `term` is floored even though `live` is newer.
    _write_run("term", "2000-01-01T00:00:00Z", overall="done")  # old terminal, past ttl
    _write_run("live", now_iso(), overall="running")  # newer, but not terminal
    assert prune_runs(Retention(keep_last=1, ttl_days=14)) == []
    assert _ids() == {"term", "live"}


def test_atomic_write_text_cleans_tmp_and_reraises_on_failure(aeview_home, tmp_path, monkeypatch):
    import aeview.runstore as rs

    target = tmp_path / "out.json"

    def boom(*_a):
        raise OSError("replace failed")

    monkeypatch.setattr(rs.os, "replace", boom)  # tmp gets written, then the rename fails
    with pytest.raises(OSError):
        rs.atomic_write_text(target, "data")
    assert list(tmp_path.glob(".out.json.*.tmp")) == []  # partial tmp cleaned up
    assert not target.exists()  # failed write did not leave a stale/partial target


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
    # keepLast=0 disables the count floor, so an aged-out run is pruned (nothing protects it).
    # A `runs[: keep_last or None]` regression (0 -> "unlimited") would keep it and fail here.
    _write_run("term", "2000-01-01T00:00:00Z")  # terminal + past ttl
    removed = prune_runs(Retention(keep_last=0, ttl_days=14))
    assert removed == ["term"]
    assert _ids() == set()


def test_prune_unparseable_created_at_is_not_too_old(aeview_home):
    # keepLast=0 (no floor): an unparseable timestamp -> _parse_ts None -> treated as "not too
    # old" -> kept, while a parseable aged-out run is pruned. Guards the `ts is not None` branch.
    _write_run("bad", "not-a-timestamp")
    _write_run("aged", "2000-01-01T00:00:00Z")
    removed = prune_runs(Retention(keep_last=0, ttl_days=14))
    assert removed == ["aged"]
    assert _ids() == {"bad"}


def test_pid_alive(aeview_home):
    assert pid_alive(os.getpid()) is True
    assert pid_alive(None) is False  # no pid recorded -> treat as dead (crashed/old run)
    assert pid_alive(_DEAD_PID) is False


def test_effective_overall_folds_in_liveness(aeview_home):
    def m(overall: str, pid: int | None) -> RunManifest:
        return RunManifest(
            run_id="x", created_at=now_iso(), overall=overall,
            invocation=Invocation(reviewers=["d"], scope=ScopeSpec(type="working-tree")),
            roster=[], pid=pid,
        )

    assert effective_overall(m("running", os.getpid())) == "running"  # live
    assert effective_overall(m("running", _DEAD_PID)) == "interrupted"  # crashed
    assert effective_overall(m("running", None)) == "interrupted"  # no pid -> crashed
    assert effective_overall(m("done", _DEAD_PID)) == "done"  # terminal unaffected


def test_reconcile_interrupted_persists_only_crashed_running(aeview_home):
    _write_run("crashed", now_iso(), overall="running", pid=_DEAD_PID)
    _write_run("alive", now_iso(), overall="running", pid=os.getpid())
    _write_run("finished", now_iso(), overall="done", pid=_DEAD_PID)
    assert reconcile_interrupted() == ["crashed"]
    states = {m.run_id: m.overall for m in list_manifests()}
    assert states["crashed"] == "interrupted"  # persisted
    assert states["alive"] == "running"  # a live run is left alone
    assert states["finished"] == "done"  # terminal unaffected


def test_reconcile_skips_run_that_finished_after_the_cached_read(aeview_home, monkeypatch):
    import aeview.runstore as rs

    # On disk the run already finished ('done'); simulate a stale cached read from _iter_run_dirs
    # (still 'running', dead pid). The re-read guard must see 'done' and NOT clobber it.
    store = RunStore.create("finished")
    store.write_manifest(
        RunManifest(
            run_id="finished", created_at=now_iso(), overall="done",
            invocation=Invocation(reviewers=["d"], scope=ScopeSpec(type="working-tree")),
            roster=[], pid=_DEAD_PID,
        )
    )
    stale = RunManifest(
        run_id="finished", created_at=now_iso(), overall="running",
        invocation=Invocation(reviewers=["d"], scope=ScopeSpec(type="working-tree")),
        roster=[], pid=_DEAD_PID,
    )
    monkeypatch.setattr(rs, "_iter_run_dirs", lambda: iter([(store.dir, stale)]))
    assert reconcile_interrupted() == []  # re-read saw 'done' -> not reconciled
    assert store.read_manifest().overall == "done"  # preserved


def test_claim_run_refuses_a_live_holder_and_releases(aeview_home):
    store = RunStore.create("locked")
    with claim_run(store):  # noqa: SIM117 - the outer claim must be held while the inner is tried
        # our own (live) pid holds it -> a second claim is refused
        with pytest.raises(RunBusy), claim_run(store):
            pass
    with claim_run(store):  # released on exit -> claimable again
        pass


def test_claim_run_steals_a_stale_lock(aeview_home):
    store = RunStore.create("stale")
    (store.dir / ".lock").write_text(str(_DEAD_PID))  # holder pid is dead
    with claim_run(store):  # stale lock stolen, no RunBusy
        assert (store.dir / ".lock").read_text() == str(os.getpid())


def test_retention_rejects_nonpositive_bounds_at_the_config_boundary():
    # The validation IS the only guard between a settings.json typo and total history loss.
    with pytest.raises(ValidationError):
        Retention(ttl_days=0)  # 0 would make cutoff==now and prune every terminal run
    with pytest.raises(ValidationError):
        Retention(keep_last=-1)
    with pytest.raises(ValidationError):
        Settings.model_validate({"retention": {"ttlDays": 0}})  # through settings.json shape
