from __future__ import annotations

import json

from typer.testing import CliRunner

from aeview.cli import app
from aeview.runstore import RunStore
from aeview.schema import (
    Coverage,
    Dedup,
    Invocation,
    Report,
    ReviewResult,
    RosterEntry,
    RunManifest,
    ScopeSpec,
    UsageBreakdown,
)

runner = CliRunner()

_REVIEW_ID = "default__claude-code-opus"


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
) -> None:
    store = RunStore.create(run_id)
    store.write_manifest(
        RunManifest(
            run_id=run_id,
            created_at=created_at,
            overall=overall,
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=_roster(),
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


def test_status_unknown_run_exits_error(aeview_home):
    res = runner.invoke(app, ["status", "nope"])
    assert res.exit_code == 2
    assert "not found" in res.output


def test_status_no_runs_exits_error(aeview_home):
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 2
    assert "no runs" in res.output


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


def test_list_json(aeview_home):
    _write_run("r", verdict="approve", contributed=1)
    res = runner.invoke(app, ["list", "--json"])
    data = json.loads(res.output)
    assert data[0]["run_id"] == "r"
    assert data[0]["verdict"] == "approve"
    assert data[0]["coverage"] == {"contributed": 1, "failed": 0}
