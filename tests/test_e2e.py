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
from conftest import commit, git, make_reviewer


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


def test_plan_filters_non_ascii_path(aeview_home, git_repo):
    # quotePath=false keeps a non-ASCII filename raw in the diff, so .aeviewignore still matches it.
    commit(git_repo, ".aeviewignore", "*.lock\n", "add ignore")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (git_repo / "café.lock").write_text("lock\n")
    plan = _plan_run(
        ["default"], "working-tree", None, git_repo, False, False, None, load_settings()
    )
    assert "café.lock" in plan.ignored
    assert "café.lock" not in plan.bundle.diff


def test_plan_filters_untracked_from_subdirectory(aeview_home, git_repo):
    # Invoked from a subdir: untracked diffs are repo-root-relative, so a root-level ignored file
    # is still matched and a kept subdir file carries its repo-root path.
    commit(git_repo, ".aeviewignore", "*.lock\n", "add ignore")
    sub = git_repo / "src"
    sub.mkdir()
    (sub / "feature.py").write_text("x = 1\n")
    (git_repo / "uv.lock").write_text("lock\n")
    plan = _plan_run(["default"], "working-tree", None, sub, False, False, None, load_settings())
    assert plan.ignored == ["uv.lock"]
    assert "b/src/feature.py" in plan.bundle.diff


@pytest.mark.parametrize("cfg", ["diff.mnemonicprefix", "diff.noprefix"])
def test_plan_filters_under_hostile_diff_config(aeview_home, git_repo, cfg):
    # A repo gitconfig that drops/renames the a//b/ prefixes must not defeat filtering — the forced
    # _GIT_BASE flags override it so the diff parser always sees standard prefixes.
    git(git_repo, "config", cfg, "true")
    commit(git_repo, ".aeviewignore", "*.lock\n", "add ignore")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (git_repo / "uv.lock").write_text("lock\n")
    plan = _plan_run(
        ["default"], "working-tree", None, git_repo, False, False, None, load_settings()
    )
    assert "uv.lock" in plan.ignored
    assert "uv.lock" not in plan.bundle.diff


def test_plan_filters_commit_scope(aeview_home, git_repo):
    # A single commit touching both an ignored and a kept file: git show -> filter through planning.
    commit(git_repo, ".aeviewignore", "*.lock\n", "add ignore")
    (git_repo / "uv.lock").write_text("lock\n")
    (git_repo / "mod.py").write_text("x = 1\n")
    git(git_repo, "add", "uv.lock", "mod.py")
    git(git_repo, "commit", "-q", "--no-verify", "-m", "both")
    sha = git(git_repo, "rev-parse", "HEAD").strip()
    plan = _plan_run(["default"], "commit", sha, git_repo, False, False, None, load_settings())
    assert plan.ignored == ["uv.lock"]
    assert "b/mod.py" in plan.bundle.diff and "b/uv.lock" not in plan.bundle.diff


_HARNESS = [{"harness": "claude-code", "model": "opus"}]


def _auto_plan(cwd):
    # Bare `aeview run` (no --reviewers) -> auto mode: default + path-activated reviewers.
    return _plan_run(None, "working-tree", None, cwd, False, False, None, load_settings())


def test_auto_mode_activates_matching_reviewer(aeview_home, git_repo):
    make_reviewer(git_repo, "py", harnesses=_HARNESS, auto_activate_paths=["*.py"])
    (git_repo / "feature.py").write_text("x = 1\n")
    plan = _auto_plan(git_repo)
    assert "default" in plan.names and "py" in plan.names
    assert plan.auto_activated == ["py"]


def test_auto_mode_default_only_when_nothing_matches(aeview_home, git_repo):
    make_reviewer(git_repo, "py", harnesses=_HARNESS, auto_activate_paths=["nomatch/**"])
    (git_repo / "feature.py").write_text("x = 1\n")
    plan = _auto_plan(git_repo)
    assert plan.names == ["default"]  # default is the always-on baseline
    assert plan.auto_activated == []


def test_explicit_default_bypasses_activation(aeview_home, git_repo):
    # `--reviewers default` is explicit -> activation is disabled even though py's glob matches.
    make_reviewer(git_repo, "py", harnesses=_HARNESS, auto_activate_paths=["*.py"])
    (git_repo / "feature.py").write_text("x = 1\n")
    plan = _plan_run(
        ["default"], "working-tree", None, git_repo, False, False, None, load_settings()
    )
    assert plan.names == ["default"]
    assert plan.auto_activated == []


def test_all_bypasses_activation(aeview_home, git_repo):
    # `all` sweeps every discovered reviewer regardless of paths, and reports no auto-activation.
    make_reviewer(git_repo, "py", harnesses=_HARNESS, auto_activate_paths=["nomatch/**"])
    (git_repo / "feature.py").write_text("x = 1\n")
    plan = _plan_run(["all"], "working-tree", None, git_repo, False, False, None, load_settings())
    assert {"default", "py"} <= set(plan.names)
    assert plan.auto_activated == []


def test_auto_fails_fast_on_broken_default(aeview_home, git_repo):
    # A broken default shadows the seeded one (nearest rung wins); auto mode must fail fast on it.
    bad = git_repo / ".aeview" / "reviewers" / "default"
    bad.mkdir(parents=True)
    (bad / "REVIEWER.md").write_text("---\nname: default\nbogus_key: 1\n---\nb\n")
    (git_repo / "feature.py").write_text("x = 1\n")
    with pytest.raises(ResolveError):
        _auto_plan(git_repo)


def test_auto_lenient_skips_broken_activated(aeview_home, git_repo):
    # A path-matched reviewer with broken config (dir name != frontmatter name) is skipped, while a
    # valid matched sibling still runs and default is unaffected.
    paths = ["*.py"]
    make_reviewer(git_repo, "good", harnesses=_HARNESS, auto_activate_paths=paths)
    make_reviewer(git_repo, "bad", fm_name="WRONG", harnesses=_HARNESS, auto_activate_paths=paths)
    (git_repo / "feature.py").write_text("x = 1\n")
    plan = _auto_plan(git_repo)
    assert "good" in plan.names and "default" in plan.names
    assert "bad" not in plan.names
    assert plan.auto_activated == ["good"]


def test_auto_activation_respects_aeviewignore(aeview_home, git_repo):
    # The only file 'locks' would match is excluded by .aeviewignore, so it doesn't auto-activate;
    # the kept .py file keeps the scope non-empty and default still runs.
    commit(git_repo, ".aeviewignore", "*.lock\n", "ignore")
    make_reviewer(git_repo, "locks", harnesses=_HARNESS, auto_activate_paths=["*.lock"])
    (git_repo / "uv.lock").write_text("x\n")
    (git_repo / "feature.py").write_text("y = 1\n")
    plan = _auto_plan(git_repo)
    assert plan.auto_activated == []
    assert "default" in plan.names


def test_auto_activates_repo_reviewer_from_subdirectory(aeview_home, git_repo):
    # Invoked from repo/src: discovery walks up to the repo-root reviewer, and changed paths anchor
    # at the repo root (not cwd), so a repo-level reviewer still activates.
    make_reviewer(git_repo, "py", harnesses=_HARNESS, auto_activate_paths=["src/**"])
    sub = git_repo / "src"
    sub.mkdir()
    (sub / "feature.py").write_text("x = 1\n")
    plan = _plan_run(None, "working-tree", None, sub, False, False, None, load_settings())
    assert "py" in plan.names
    assert plan.auto_activated == ["py"]


def test_auto_activates_for_commit_scope(aeview_home, git_repo):
    # Auto mode skips only patch scope; a commit-scope bare run still activates from its diff.
    make_reviewer(git_repo, "py", harnesses=_HARNESS, auto_activate_paths=["*.py"])
    sha = commit(git_repo, "added.py", "x = 1\n", "add py")
    plan = _plan_run(None, "commit", sha, git_repo, False, False, None, load_settings())
    assert "py" in plan.names
    assert plan.auto_activated == ["py"]


def test_auto_repo_default_with_matching_paths_runs_once(aeview_home, git_repo):
    # A repo-level `default` whose own auto-activate-paths match must resolve exactly once — never
    # duplicated into a second roster entry with a clashing id.
    make_reviewer(git_repo, "default", harnesses=_HARNESS, auto_activate_paths=["*.py"])
    (git_repo / "feature.py").write_text("x = 1\n")
    plan = _auto_plan(git_repo)
    assert plan.names == ["default"]
    assert [e.reviewer for e in plan.roster].count("default") == 1
    assert plan.auto_activated == []  # default is the baseline, never reported as an extra


def test_auto_patch_scope_runs_default_only(aeview_home, git_repo):
    # patch-scope paths aren't repo-root-relative, so auto mode can't anchor them -> default alone.
    make_reviewer(git_repo, "py", harnesses=_HARNESS, auto_activate_paths=["*.py"])
    patch = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    plan = _plan_run(None, "patch", None, git_repo, False, False, patch, load_settings())
    assert plan.names == ["default"]
    assert plan.auto_activated == []


def test_auto_mode_full_run_activated_reviewer_contributes(aeview_home, git_repo, stub_claude):
    # End-to-end: a bare run actually executes the path-activated reviewer (not just announces it).
    make_reviewer(git_repo, "py", harnesses=_HARNESS, auto_activate_paths=["*.py"])
    (git_repo / "feature.py").write_text("x = 1\n")
    report = asyncio.run(_orchestrate(None, "working-tree", None, git_repo, False, False, None))
    assert report.coverage.contributed == 2  # default + the path-activated py
    run = next(iter(runs_dir().iterdir()))
    manifest = json.loads((run / "run.json").read_text())
    assert "py" in {e["reviewer"] for e in manifest["roster"]}


def test_scope_error_precedes_unknown_reviewer_error(aeview_home, git_repo):
    # The reordering contract: scope is resolved before reviewers, so an empty diff surfaces a
    # ScopeError even when the named reviewer is also unknown.
    with pytest.raises(ScopeError):
        _plan_run(["nope"], "working-tree", None, git_repo, False, False, None, load_settings())


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
