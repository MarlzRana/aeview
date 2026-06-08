from __future__ import annotations

import json
import os

from typer.testing import CliRunner

from aeview import fanout
from aeview.cli import app
from aeview.harness.base import HarnessOutput
from aeview.runstore import RunStore
from aeview.schema import (
    Coverage,
    Dedup,
    DedupPlan,
    Finding,
    Invocation,
    Location,
    Report,
    ReviewOutput,
    ReviewResult,
    RosterEntry,
    RunManifest,
    ScopeSpec,
    Usage,
    UsageBreakdown,
)

runner = CliRunner()

_REVIEW_ID = "default__claude-code-opus"
_DEAD_PID = 999_999  # fits pid_t, ~never a live process -> reads as a crashed run


class _ApproveAdapter:
    """A stub harness that approves with no findings — used to drive resume's re-run."""

    async def run(self, prompt, model, cwd, log_path, thinking=None, timeout=None):
        return HarnessOutput(
            review=ReviewOutput(verdict="approve", summary="ok", findings=[], next_steps=[]),
            usage=Usage(),
            raw="{}",
        )


def _roster() -> list[RosterEntry]:
    return [
        RosterEntry(id=_REVIEW_ID, reviewer="default", harness="claude-code", model="opus")
    ]


def _write_run(
    run_id: str,
    *,
    created_at: str = "2026-06-07T10:00:00Z",
    overall: str = "done",
    review_status: str | None = "done",
    with_report: bool = True,
    verdict: str = "needs-attention",
    contributed: int = 1,
    failed: int = 0,
    pid: int | None = None,
) -> None:
    store = RunStore.create(run_id)
    store.write_manifest(
        RunManifest(
            run_id=run_id,
            created_at=created_at,
            overall=overall,
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=_roster(),
            # A live pid by default so a 'running' fixture reads as live (not crashed/interrupted);
            # liveness treats a missing/dead pid as interrupted.
            pid=os.getpid() if pid is None else pid,
        )
    )
    if review_status is not None:
        store.write_review(
            ReviewResult(
                id=_REVIEW_ID,
                reviewer="default",
                harness="claude-code",
                model="opus",
                status=review_status,
            )
        )
    if with_report:
        store.write_report(
            Report(
                verdict=verdict,
                summary="s",
                coverage=Coverage(contributed=contributed, failed=failed),
                dedup=Dedup(status="skipped"),
                usage=UsageBreakdown(),
            )
        )


# --- status ---


def test_status_defaults_to_latest_run(aeview_home):
    _write_run("older", created_at="2026-06-07T09:00:00Z")
    _write_run("newer", created_at="2026-06-07T12:00:00Z", overall="running", with_report=False)
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0
    assert "newer" in res.output and "older" not in res.output


def test_status_unstarted_review_is_pending(aeview_home):
    _write_run("r", overall="running", review_status=None, with_report=False)
    res = runner.invoke(app, ["status", "r"])
    assert "[pending]" in res.output
    assert "1 pending (of 1)" in res.output


def test_status_reports_done_state(aeview_home):
    _write_run("r")
    res = runner.invoke(app, ["status", "r"])
    assert "[done]" in res.output
    assert "state: done" in res.output


def test_status_json_shape(aeview_home):
    _write_run("r")
    res = runner.invoke(app, ["status", "r", "--json"])
    data = json.loads(res.output)
    assert data["run_id"] == "r"
    assert data["overall"] == "done"
    assert data["counts"] == {"done": 1}
    assert data["reviews"][0]["id"] == _REVIEW_ID
    # scope + timestamp keys are part of the JSON contract automation consumes.
    assert data["scope"] == {"type": "working-tree", "base": None}
    assert data["created_at"] == "2026-06-07T10:00:00Z"
    assert "started_at" in data and "finished_at" in data


def test_status_unknown_run_exits_error(aeview_home):
    res = runner.invoke(app, ["status", "nope"])
    assert res.exit_code == 2
    assert "not found" in res.output


def test_status_no_runs_exits_error(aeview_home):
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 2
    assert "no runs" in res.output


def test_status_uses_directory_not_manifest_run_id(aeview_home):
    # The dir is authoritative: a run.json whose run_id names another ("ghost") dir must not
    # redirect reads. status reads realdir's reviews and reports realdir as the id.
    store = RunStore.create("realdir")
    store.write_manifest(
        RunManifest(
            run_id="ghost",
            created_at="2026-06-07T10:00:00Z",
            overall="done",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=_custom_roster(_REVIEW_ID),
        )
    )
    store.write_review(
        ReviewResult(
            id=_REVIEW_ID, reviewer="default", harness="claude-code", model="m", status="done"
        )
    )
    data = json.loads(runner.invoke(app, ["status", "realdir", "--json"]).output)
    assert data["run_id"] == "realdir"  # not the manifest's "ghost"
    assert data["counts"] == {"done": 1}  # reviews read from realdir, not "ghost"


def test_status_rejects_traversal_run_id(aeview_home):
    res = runner.invoke(app, ["status", "../escape"])
    assert res.exit_code == 2
    assert "not found" in res.output


def test_status_rejects_bare_dot_run_id(aeview_home):
    # A bare '.' / '..' has no slash, so it must be caught by the set-membership guard.
    res = runner.invoke(app, ["status", "."])
    assert res.exit_code == 2
    assert "not found" in res.output


def test_status_rejects_dotdot_and_backslash_run_ids(aeview_home):
    # bare '..' (set-membership only) and a backslash (the Windows-path-sep half of the guard).
    assert runner.invoke(app, ["status", ".."]).exit_code == 2
    assert runner.invoke(app, ["status", "a\\b"]).exit_code == 2


def _custom_roster(*ids: str) -> list[RosterEntry]:
    return [
        RosterEntry(id=i, reviewer="default", harness="claude-code", model="m") for i in ids
    ]


def _review(rid: str, status: str) -> ReviewResult:
    return ReviewResult(id=rid, reviewer="default", harness="claude-code", model="m", status=status)


def test_status_mixed_review_states_counts_and_order(aeview_home):
    roster = _custom_roster("default__a", "default__b", "default__c", "default__d")
    store = RunStore.create("mixed")
    store.write_manifest(
        RunManifest(
            run_id="mixed",
            created_at="2026-06-07T10:00:00Z",
            overall="running",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=roster,
        )
    )
    for rid, st in [("default__a", "done"), ("default__b", "failed"), ("default__c", "running")]:
        store.write_review(
            ReviewResult(id=rid, reviewer="default", harness="claude-code", model="m", status=st)
        )
    # default__d has no review.json -> pending
    res = runner.invoke(app, ["status", "mixed"])
    assert res.exit_code == 0
    assert "1 done, 1 running, 1 pending, 1 failed (of 4)" in res.output  # _REVIEW_STATUS_ORDER
    assert "[failed] default__b" in res.output
    assert "[running] default__c" in res.output


def test_status_empty_roster_fallback(aeview_home):
    store = RunStore.create("empty")
    store.write_manifest(
        RunManifest(
            run_id="empty",
            created_at="2026-06-07T10:00:00Z",
            overall="done",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=[],
        )
    )
    res = runner.invoke(app, ["status", "empty"])
    assert "reviews: 0 (of 0)" in res.output


def test_status_survives_corrupt_review_json(aeview_home):
    store = RunStore.create("r")
    store.write_manifest(
        RunManifest(
            run_id="r",
            created_at="2026-06-07T10:00:00Z",
            overall="running",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=_custom_roster(_REVIEW_ID),
        )
    )
    bad_dir = store.reviewers_dir / "default" / "claude-code-opus"
    bad_dir.mkdir(parents=True)
    (bad_dir / "review.json").write_text("{not valid json")
    res = runner.invoke(app, ["status", "r"])
    assert res.exit_code == 0  # a corrupt review.json is skipped, not fatal
    assert f"[pending] {_REVIEW_ID}" in res.output


# --- result ---


def test_result_needs_attention_exits_1(aeview_home):
    _write_run("r", verdict="needs-attention", contributed=1)
    res = runner.invoke(app, ["result", "r"])
    assert res.exit_code == 1
    assert "needs-attention" in res.output


def test_result_approve_exits_0(aeview_home):
    _write_run("r", verdict="approve", contributed=1)
    res = runner.invoke(app, ["result", "r"])
    assert res.exit_code == 0


def test_result_all_failed_exits_error(aeview_home):
    _write_run("r", verdict="approve", contributed=0, failed=1)
    res = runner.invoke(app, ["result", "r"])
    assert res.exit_code == 2  # no contributing review -> error, not a green approve
    # the human render must also show error, not the stored (false-green) "approve"
    assert "error" in res.output
    assert "approve" not in res.output


def test_result_no_report_yet_exits_error(aeview_home):
    _write_run("r", overall="running", with_report=False)
    res = runner.invoke(app, ["result", "r"])
    assert res.exit_code == 2
    assert "no report yet" in res.output


def test_result_json(aeview_home):
    _write_run("r", verdict="needs-attention")
    res = runner.invoke(app, ["result", "r", "--json"])
    data = json.loads(res.output)
    assert data["verdict"] == "needs-attention"


def test_result_defaults_to_latest_run(aeview_home):
    _write_run("older", created_at="2026-06-07T09:00:00Z", verdict="approve", contributed=1)
    _write_run("newer", created_at="2026-06-07T12:00:00Z", verdict="needs-attention", contributed=1)
    res = runner.invoke(app, ["result"])
    assert res.exit_code == 1  # latest ("newer") is needs-attention
    assert "needs-attention" in res.output


def test_result_empty_run_id_errors_not_latest(aeview_home):
    # An explicit empty id (e.g. an empty shell var) must error, not silently read the latest run.
    _write_run("r", verdict="approve", contributed=1)
    res = runner.invoke(app, ["result", ""])
    assert res.exit_code == 2
    assert "not found" in res.output


# --- list ---


def test_list_empty(aeview_home):
    res = runner.invoke(app, ["list"])
    assert "no runs" in res.output


def test_list_newest_first_with_verdict_and_coverage(aeview_home):
    _write_run("old", created_at="2026-06-07T09:00:00Z", verdict="approve", contributed=1)
    _write_run(
        "new", created_at="2026-06-07T13:00:00Z", verdict="needs-attention",
        contributed=2, failed=1,
    )
    res = runner.invoke(app, ["list"])
    lines = [ln for ln in res.output.splitlines() if ln.strip()]
    assert lines[0].startswith("new")  # newest first
    assert "needs-attention" in lines[0]
    assert "2 contributed, 1 failed" in lines[0]


def test_list_running_run_shows_state_not_verdict(aeview_home):
    _write_run("r", overall="running", with_report=False)
    res = runner.invoke(app, ["list"])
    assert "running" in res.output


def test_list_all_failed_run_shows_error_not_stored_verdict(aeview_home):
    # report verdict says "approve" but zero reviews contributed -> the row must show "error".
    _write_run("r", verdict="approve", contributed=0, failed=1)
    res = runner.invoke(app, ["list"])
    assert "error" in res.output
    assert "approve" not in res.output  # the false-green is overridden
    data = json.loads(runner.invoke(app, ["list", "--json"]).output)
    assert data[0]["verdict"] == "error"


def test_list_terminal_run_without_report_falls_back_to_state(aeview_home):
    # A terminal run whose report.json is missing falls back to the manifest state; coverage "-".
    _write_run("r", overall="failed", with_report=False)
    res = runner.invoke(app, ["list"])
    line = next(ln for ln in res.output.splitlines() if ln.startswith("r "))
    assert "failed" in line
    assert line.rstrip().endswith("-")


def test_list_terminal_run_with_corrupt_report_falls_back(aeview_home):
    # A corrupt report.json (ValueError, not OSError) must fall back, not crash `list`.
    _write_run("r", overall="done", with_report=False)
    (RunStore("r").dir / "report.json").write_text("{not valid json")
    res = runner.invoke(app, ["list"])
    line = next(ln for ln in res.output.splitlines() if ln.startswith("r "))
    assert "done" in line
    assert line.rstrip().endswith("-")


def test_list_json(aeview_home):
    _write_run("r", verdict="approve", contributed=1)
    res = runner.invoke(app, ["list", "--json"])
    data = json.loads(res.output)
    assert data[0]["run_id"] == "r"
    assert data[0]["verdict"] == "approve"
    assert data[0]["coverage"] == {"contributed": 1, "failed": 0}


def test_list_uses_directory_not_manifest_run_id(aeview_home):
    # list reads each run's report via the enumerated dir, not the manifest's self-declared id.
    store = RunStore.create("realdir2")
    store.write_manifest(
        RunManifest(
            run_id="ghost2",
            created_at="2026-06-07T10:00:00Z",
            overall="done",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=_custom_roster(_REVIEW_ID),
        )
    )
    store.write_report(
        Report(
            verdict="approve",
            summary="s",
            coverage=Coverage(contributed=1, failed=0),
            dedup=Dedup(status="skipped"),
            usage=UsageBreakdown(),
        )
    )
    row = json.loads(runner.invoke(app, ["list", "--json"]).output)[0]
    assert row["run_id"] == "realdir2"  # not "ghost2"
    assert row["verdict"] == "approve"  # report read from realdir2, not the absent "ghost2"


# --- status --wait ---


def test_status_wait_finished_run_exits_verdict(aeview_home):
    _write_run("r", verdict="approve", contributed=1)  # done + report present
    res = runner.invoke(app, ["status", "r", "--wait"])
    assert res.exit_code == 0  # already terminal -> adopts the run's verdict (approve)


def test_status_wait_needs_attention_exits_1(aeview_home):
    _write_run("r", verdict="needs-attention", contributed=1)
    res = runner.invoke(app, ["status", "r", "--wait"])
    assert res.exit_code == 1


def test_status_wait_crashed_run_exits_error(aeview_home):
    # running + dead pid -> liveness treats it as terminal (interrupted); no report -> exit 2.
    _write_run("r", overall="running", with_report=False, pid=_DEAD_PID)
    res = runner.invoke(app, ["status", "r", "--wait"])
    assert res.exit_code == 2
    assert "interrupted" in res.output


# --- resume ---


def _resume_run(run_id: str, statuses: dict[str, str]) -> RunStore:
    """A run dir with a 2-entry roster, the given per-review statuses, and persisted prompts."""
    store = RunStore.create(run_id)
    store.write_prompt("default", "PROMPT")
    roster = [
        RosterEntry(id="default__a", reviewer="default", harness="claude-code", model="a"),
        RosterEntry(id="default__b", reviewer="default", harness="claude-code", model="b"),
    ]
    store.write_manifest(
        RunManifest(
            run_id=run_id,
            created_at="2026-06-07T10:00:00Z",
            overall="interrupted",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=roster,
            dedup=None,  # 2 reviews but no findings -> dedup skipped, no harness call needed
            pid=_DEAD_PID,
        )
    )
    for rid, status in statuses.items():
        store.write_review(
            ReviewResult(
                id=rid, reviewer="default", harness="claude-code",
                model=rid.split("__")[1], status=status,
                verdict="approve" if status == "done" else None, summary="s",
            )
        )
    return store


def test_resume_reruns_only_non_done_and_remerges(aeview_home, monkeypatch):
    monkeypatch.setattr(fanout, "get_adapter", lambda h: _ApproveAdapter())
    store = _resume_run("rerun", {"default__a": "done", "default__b": "failed"})
    res = runner.invoke(app, ["resume", "rerun"])
    assert res.exit_code == 0  # both approve now -> merged approve
    reviews = {r.id: r for r in store.read_reviews()}
    assert reviews["default__a"].status == "done" and reviews["default__b"].status == "done"
    # The already-done review must NOT be re-run (re-billing it): its original "s" summary
    # survives, while the re-run b carries the stub's "ok". A regression that re-ran the whole
    # roster would overwrite a's summary with "ok".
    assert reviews["default__a"].summary == "s"
    assert reviews["default__b"].summary == "ok"
    assert store.read_report().coverage.contributed == 2


def test_resume_merge_only_when_crashed_before_merge(aeview_home, monkeypatch):
    # All reviews done but no report (crashed after the reviews, before merge): resume merges
    # the on-disk reviews without re-running anything.
    monkeypatch.setattr(fanout, "get_adapter", lambda h: _ApproveAdapter())  # must NOT be called
    store = _resume_run("premerge", {"default__a": "done", "default__b": "done"})
    assert not (store.dir / "report.json").exists()
    res = runner.invoke(app, ["resume", "premerge"])
    assert res.exit_code == 0
    assert store.read_report().coverage.contributed == 2  # merged from disk
    assert {r.summary for r in store.read_reviews()} == {"s"}  # neither was re-run


def test_status_wait_polls_until_terminal(aeview_home, monkeypatch):
    import aeview.cli as cli

    # Start live (running + our pid) so --wait enters the loop; the first poll's sleep flips the
    # manifest to done on disk, so the loop re-reads, sees terminal, and exits with the verdict.
    store = RunStore.create("r")
    store.write_manifest(
        RunManifest(
            run_id="r", created_at="2026-06-07T10:00:00Z", overall="running",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=_custom_roster(_REVIEW_ID), pid=os.getpid(),
        )
    )
    store.write_review(
        ReviewResult(id=_REVIEW_ID, reviewer="default", harness="claude-code", model="opus",
                     status="done")
    )
    store.write_report(
        Report(verdict="approve", summary="s", coverage=Coverage(contributed=1, failed=0),
               dedup=Dedup(status="skipped"), usage=UsageBreakdown())
    )

    def flip_to_done(_seconds):
        m = store.read_manifest()
        m.overall = "done"
        store.write_manifest(m)

    monkeypatch.setattr(cli.time, "sleep", flip_to_done)  # one poll -> terminal (no real wait)
    res = runner.invoke(app, ["status", "r", "--wait"])
    assert res.exit_code == 0  # looped once, re-read terminal, adopted the approve verdict


def test_resume_refuses_a_live_run(aeview_home):
    store = RunStore.create("live")
    store.write_manifest(
        RunManifest(
            run_id="live",
            created_at="2026-06-07T10:00:00Z",
            overall="running",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=_custom_roster(_REVIEW_ID),
            pid=os.getpid(),  # alive
        )
    )
    res = runner.invoke(app, ["resume", "live"])
    assert res.exit_code == 2
    assert "still running" in res.output


def test_resume_already_complete_is_a_noop(aeview_home):
    _write_run("done1", verdict="approve", contributed=1)  # all reviews done + report present
    res = runner.invoke(app, ["resume", "done1"])
    assert res.exit_code == 0
    assert "already complete" in res.output


# --- resume: cwd / frozen prompt / pinned dedup / timeout / lock (I6b-1 dogfood r2) ---


class _RecordingAdapter:
    """Records the cwd + prompt each review is run with (and approves)."""

    def __init__(self):
        self.seen: list[dict] = []

    async def run(self, prompt, model, cwd, log_path, thinking=None, timeout=None):
        self.seen.append({"prompt": prompt, "cwd": str(cwd)})
        return HarnessOutput(
            review=ReviewOutput(verdict="approve", summary="ok", findings=[], next_steps=[]),
            usage=Usage(), raw="{}",
        )


def test_resume_uses_manifest_cwd_and_frozen_prompt(aeview_home, tmp_path, monkeypatch):
    adapter = _RecordingAdapter()
    monkeypatch.setattr(fanout, "get_adapter", lambda h: adapter)
    repo = tmp_path / "original-repo"
    repo.mkdir()
    store = RunStore.create("rc")
    store.write_prompt("default", "FROZEN PROMPT BODY")
    store.write_manifest(
        RunManifest(
            run_id="rc", created_at="2026-06-07T10:00:00Z", overall="interrupted",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=_custom_roster("default__a"), dedup=None, cwd=str(repo), pid=_DEAD_PID,
        )
    )
    store.write_review(_review("default__a", "running"))
    res = runner.invoke(app, ["resume", "rc"])
    assert res.exit_code == 0
    assert adapter.seen[0]["cwd"] == str(repo)  # the recorded repo, not the caller's cwd
    assert adapter.seen[0]["prompt"] == "FROZEN PROMPT BODY"  # the persisted prompt, re-used


class _DedupStubAdapter:
    """Each review returns one finding; the dedup call returns no groups."""

    async def run(self, prompt, model, cwd, log_path, thinking=None, timeout=None):
        finding = Finding(
            title="t", body="b", severity="low", category="bug", confidence=0.5,
            location=Location(file="f.py", line_start=1, line_end=1), recommendation="r",
        )
        return HarnessOutput(
            review=ReviewOutput(verdict="needs-attention", summary="s", findings=[finding],
                                next_steps=[]),
            usage=Usage(), raw="{}",
        )

    async def run_structured(self, prompt, schema, model, cwd, log_path, thinking=None,
                             timeout=None, validate=None):
        from aeview.harness.base import StructuredOutput

        return StructuredOutput(payload={"duplicate_groups": []}, usage=Usage(), raw="{}")


def test_resume_remerges_via_pinned_dedup_harness(aeview_home, monkeypatch):
    import aeview.dedup as dedup_mod

    monkeypatch.setattr(fanout, "get_adapter", lambda h: _DedupStubAdapter())
    monkeypatch.setattr(dedup_mod, "get_adapter", lambda h: _DedupStubAdapter())
    store = RunStore.create("pin")
    store.write_prompt("default", "P")
    roster = [
        RosterEntry(id="default__a", reviewer="default", harness="claude-code", model="a"),
        RosterEntry(id="default__b", reviewer="default", harness="claude-code", model="b"),
    ]
    store.write_manifest(
        RunManifest(
            run_id="pin", created_at="2026-06-07T10:00:00Z", overall="interrupted",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=roster,
            dedup=DedupPlan(id="codex-gpt-5.5", harness="codex", model="gpt-5.5", thinking=None),
            pid=_DEAD_PID,
        )
    )
    runner.invoke(app, ["resume", "pin"])
    report = store.read_report()
    # Dedup fired (2 reviews, 2 findings) using the run.json-PINNED harness, not settings.json's.
    assert report.dedup.status == "ok"
    assert report.dedup.harness == "codex-gpt-5.5"


def test_resume_passes_configured_timeout_to_fan_out(aeview_home, monkeypatch):
    import aeview.cli as cli

    aeview_home.mkdir(parents=True, exist_ok=True)
    (aeview_home / "settings.json").write_text(json.dumps({"reviewTimeoutSeconds": 777}))
    captured: dict = {}

    async def fake_fan_out(store, roster, prompts, cwd, timeout=None):
        captured["timeout"] = timeout
        return []

    monkeypatch.setattr(cli, "fan_out", fake_fan_out)
    store = RunStore.create("t")
    store.write_prompt("default", "P")
    store.write_manifest(
        RunManifest(
            run_id="t", created_at="2026-06-07T10:00:00Z", overall="interrupted",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=_custom_roster("default__a"), dedup=None, pid=_DEAD_PID,
        )
    )
    store.write_review(_review("default__a", "running"))
    runner.invoke(app, ["resume", "t"])
    assert captured["timeout"] == 777  # the configured per-review timeout reaches the fan-out


def test_resume_missing_prompt_exits_error(aeview_home):
    store = RunStore.create("noprompt")
    store.write_manifest(
        RunManifest(
            run_id="noprompt", created_at="2026-06-07T10:00:00Z", overall="interrupted",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=_custom_roster("default__a"), dedup=None, pid=_DEAD_PID,
        )
    )
    store.write_review(_review("default__a", "failed"))  # pending, but no prompt.md was written
    res = runner.invoke(app, ["resume", "noprompt"])
    assert res.exit_code == 2
    assert "cannot resume" in res.output


def test_resume_clears_stale_report_before_rerunning(aeview_home, monkeypatch):
    # A crash mid-resume must not leave the old verdict readable: report.json is dropped before
    # the pending reviews are re-run (so result / status --wait can't serve the stale report).
    import aeview.cli as cli

    store = _resume_run("stale", {"default__a": "done", "default__b": "failed"})
    store.write_report(
        Report(verdict="approve", summary="stale", coverage=Coverage(contributed=2, failed=0),
               dedup=Dedup(status="skipped"), usage=UsageBreakdown())
    )
    seen: dict = {}

    async def fake_fan_out(s, roster, prompts, cwd, timeout=None):
        seen["report_existed"] = (s.dir / "report.json").exists()
        return []

    monkeypatch.setattr(cli, "fan_out", fake_fan_out)
    runner.invoke(app, ["resume", "stale"])
    assert seen["report_existed"] is False  # cleared before the re-run started
