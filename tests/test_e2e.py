from __future__ import annotations

import asyncio
import json

import pytest

from aeview.cli import _execute, _plan_run
from aeview.config import load_settings, runs_dir
from aeview.report import EXIT_APPROVE, EXIT_ERROR, EXIT_NEEDS_ATTENTION, exit_code
from aeview.resolve import ResolveError
from aeview.schema import Report
from aeview.scope import ScopeError
from conftest import commit, make_reviewer


async def _orchestrate(names, stype, value, cwd, include_dirty, allow_conflicts, patch_text):
    # The full run path = plan (sync, raises ScopeError/ResolveError) then execute (async).
    settings = load_settings()
    plan = _plan_run(names, stype, value, cwd, include_dirty, allow_conflicts, patch_text, settings)
    return await _execute(plan, settings, cwd)


def _run(repo):
    return asyncio.run(
        _orchestrate(["default"], "working-tree", None, repo, False, False, None)
    )


def test_e2e_needs_attention(aeview_home, git_repo, stub_claude):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    report = _run(git_repo)

    assert isinstance(report, Report)
    assert report.verdict == "needs-attention"
    assert exit_code(report) == EXIT_NEEDS_ATTENTION
    assert report.coverage.contributed == 1
    assert report.findings[0].title == "Unhandled None input"

    # Run directory is consistent: one run, report.json valid, one review on disk.
    run_dirs = list(runs_dir().iterdir())
    assert len(run_dirs) == 1
    run = run_dirs[0]
    report_on_disk = Report.model_validate_json((run / "report.json").read_text())
    assert report_on_disk.verdict == "needs-attention"
    reviews = list((run / "reviewers").glob("*/*/review.json"))
    assert len(reviews) == 1
    review = json.loads(reviews[0].read_text())
    assert review["status"] == "done"
    assert review["id"] == "default__claude-code-claude-opus-4-8"
    assert (run / "bundle" / "inline_bundle.diff").exists()
    assert (run / "reviewers" / "default" / "prompt.md").exists()
    # The run records its pid (foreground too) so liveness can tell a live run from a crash, and
    # the shared completion path flips the manifest from 'running' to the terminal state.
    manifest = json.loads((run / "run.json").read_text())
    assert isinstance(manifest["pid"], int)
    assert manifest["cwd"] == str(git_repo)  # recorded so resume re-runs from the right repo
    assert manifest["overall"] == "done" and manifest["finished_at"] is not None
    instance_dir = run / "reviewers" / "default" / "claude-code-claude-opus-4-8"
    assert (instance_dir / "review.json").exists()
    assert (instance_dir / "review.log").exists()


def test_e2e_approve(aeview_home, git_repo, stub_claude):
    stub_claude("approve")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b + 0\n")
    report = _run(git_repo)
    assert report.verdict == "approve"
    assert exit_code(report) == EXIT_APPROVE


def test_e2e_harness_error_marks_failed(aeview_home, git_repo, stub_claude):
    stub_claude("error")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    report = _run(git_repo)
    assert report.coverage.contributed == 0
    assert report.coverage.failed == 1
    # Every review failed -> the run is an error, not a spurious approve.
    assert exit_code(report) == EXIT_ERROR


def test_e2e_malformed_output_marks_failed(aeview_home, git_repo, stub_claude):
    stub_claude("malformed")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    report = _run(git_repo)
    assert report.coverage.failed == 1


def test_empty_diff_raises(aeview_home, git_repo, stub_claude):
    with pytest.raises(ScopeError):
        _run(git_repo)


def test_all_changes_ignored_raises(aeview_home, git_repo, stub_claude):
    # .aeviewignore (committed, so it isn't itself in the diff) excludes the only changed file ->
    # the run has nothing left to review and says so, rather than fanning out over an empty diff.
    commit(git_repo, ".aeviewignore", "*.py\n", "add ignore")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    with pytest.raises(ScopeError, match="matched .aeviewignore"):
        _run(git_repo)


def test_plan_excludes_ignored_from_bundle(aeview_home, git_repo):
    # Mixed changes: an ignored uv.lock and a kept app.py. The bundle (and its byte count) must
    # carry only the kept file; the ignored one is recorded in plan.ignored.
    commit(git_repo, ".aeviewignore", "*.lock\n", "add ignore")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (git_repo / "uv.lock").write_text("lockfile\n")
    plan = _plan_run(
        ["default"], "working-tree", None, git_repo, False, False, None, load_settings()
    )
    assert plan.ignored == ["uv.lock"]
    assert "b/app.py" in plan.bundle.diff and "b/uv.lock" not in plan.bundle.diff
    assert plan.bundle.diff_bytes == len(plan.bundle.diff.encode("utf-8"))


def test_unknown_reviewer_raises(aeview_home, git_repo, stub_claude):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    with pytest.raises(ResolveError):
        asyncio.run(_orchestrate(["nope"], "working-tree", None, git_repo, False, False, None))


def test_e2e_dedup_runs_through_orchestrate(aeview_home, git_repo, stub_claude):
    # Two reviewers -> roster>1 -> the real orchestrate->merge->run_dedup->adapter seam runs
    # unmocked, and run.json pins the dedup plan. The stub returns empty groups for the dedup
    # call (recognized by its schema), so dedup completes "ok" without guessing finding ids.
    hp = [{"harness": "claude-code", "model": "opus"}]
    make_reviewer(git_repo, "r1", harnesses=hp)
    make_reviewer(git_repo, "r2", harnesses=hp)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")

    report = asyncio.run(
        _orchestrate(["r1", "r2"], "working-tree", None, git_repo, False, False, None)
    )

    assert report.coverage.contributed == 2
    assert report.dedup.status == "ok"
    assert report.dedup.harness == "claude-code-claude-opus-4-8"  # the seeded dedup harness

    run = next(iter(runs_dir().iterdir()))
    manifest = json.loads((run / "run.json").read_text())
    assert manifest["dedup"]["id"] == "claude-code-claude-opus-4-8"  # pinned in run.json
    assert (run / "dedup" / "claude-code-claude-opus-4-8" / "result.json").exists()
