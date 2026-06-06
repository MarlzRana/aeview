from __future__ import annotations

import asyncio
import json

import pytest

from aeview.cli import _orchestrate
from aeview.config import runs_dir
from aeview.report import EXIT_APPROVE, EXIT_ERROR, EXIT_NEEDS_ATTENTION, exit_code
from aeview.resolve import ResolveError
from aeview.schema import Report
from aeview.scope import ScopeError


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


def test_unknown_reviewer_raises(aeview_home, git_repo, stub_claude):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    with pytest.raises(ResolveError):
        asyncio.run(_orchestrate(["nope"], "working-tree", None, git_repo, False, False, None))
