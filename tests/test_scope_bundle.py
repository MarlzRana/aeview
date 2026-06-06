from __future__ import annotations

import pytest

from aeview.bundle import INLINE_MAX_BYTES, build_bundle
from aeview.scope import ScopeError, parse_scope
from aeview.scope import resolve as resolve_scope
from conftest import commit, git


def _resolve(repo, stype, value=None, **kw):
    return resolve_scope(stype, value, repo, **kw)


# --- parse_scope ----------------------------------------------------------------------


def test_parse_scope_splits_type_and_value():
    assert parse_scope("branch:origin/main") == ("branch", "origin/main")
    assert parse_scope("working-tree") == ("working-tree", None)


def test_parse_scope_unknown_type():
    with pytest.raises(ScopeError):
        parse_scope("nonsense")


def test_parse_scope_value_required():
    with pytest.raises(ScopeError):
        parse_scope("range")
    with pytest.raises(ScopeError):
        parse_scope("patch")


def test_parse_scope_rejects_option_like_value():
    # A value git would read as an option (arbitrary-file-write footgun).
    with pytest.raises(ScopeError):
        parse_scope("range:--output=/tmp/x")
    with pytest.raises(ScopeError):
        parse_scope("commit:-x")


def test_parse_scope_allows_stdin_patch_sentinel():
    assert parse_scope("patch:-") == ("patch", "-")


# --- working-tree / staged ------------------------------------------------------------


def test_working_tree_includes_unstaged_and_untracked(git_repo):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (git_repo / "new.py").write_text("print('hi')\n")
    r = _resolve(git_repo, "working-tree")
    assert "return a - b" in r.diff
    assert "new.py" in r.diff
    assert r.spec.type == "working-tree"


def test_staged_only_sees_staged(git_repo):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a * b\n")
    git(git_repo, "add", "app.py")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a * b * 1\n")  # extra unstaged
    r = _resolve(git_repo, "staged")
    assert "return a * b" in r.diff
    assert "* b * 1" not in r.diff  # unstaged edit excluded


def test_clean_tree_is_empty(git_repo):
    assert _resolve(git_repo, "working-tree").is_empty


# --- commit / range -------------------------------------------------------------------


def test_commit_default_is_head(git_repo):
    commit(git_repo, "b.py", "b = 1\n", "add b")
    r = _resolve(git_repo, "commit")
    assert "b.py" in r.diff
    assert r.spec.base == "HEAD"


def test_commit_specific_ref(git_repo):
    first = git(git_repo, "rev-parse", "HEAD").strip()
    commit(git_repo, "b.py", "b = 1\n", "add b")
    r = _resolve(git_repo, "commit", first)
    assert "app.py" in r.diff  # the initial commit's content
    assert "b.py" not in r.diff


def test_range_diff(git_repo):
    a = git(git_repo, "rev-parse", "HEAD").strip()
    b = commit(git_repo, "b.py", "b = 1\n", "add b")
    r = _resolve(git_repo, "range", f"{a}..{b}")
    assert "b.py" in r.diff


# --- branch ---------------------------------------------------------------------------


def test_branch_auto_base_diffs_only_branch_work(git_repo):
    git(git_repo, "checkout", "-q", "-b", "feature")
    commit(git_repo, "feat.py", "feat = 1\n", "add feat")
    r = _resolve(git_repo, "branch")
    assert "feat.py" in r.diff
    assert "app.py" not in r.diff  # base content excluded
    assert r.spec.base == "main"
    assert "add feat" in r.commits


def test_branch_explicit_base(git_repo):
    git(git_repo, "checkout", "-q", "-b", "feature")
    commit(git_repo, "feat.py", "feat = 1\n", "add feat")
    r = _resolve(git_repo, "branch", "main")
    assert "feat.py" in r.diff


def test_branch_include_dirty_folds_worktree(git_repo):
    git(git_repo, "checkout", "-q", "-b", "feature")
    commit(git_repo, "feat.py", "feat = 1\n", "add feat")
    (git_repo / "dirty.py").write_text("dirty = 1\n")  # untracked, uncommitted
    r = _resolve(git_repo, "branch", include_dirty=True)
    assert "feat.py" in r.diff
    assert "dirty.py" in r.diff


def test_include_dirty_rejected_on_commit(git_repo):
    with pytest.raises(ScopeError):
        _resolve(git_repo, "commit", include_dirty=True)


def test_staged_include_dirty_widens_to_working_tree(git_repo):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a * b\n")
    git(git_repo, "add", "app.py")  # staged change A
    (git_repo / "app.py").write_text("def add(a, b):\n    return a * b * 1\n")  # unstaged B
    (git_repo / "untracked.py").write_text("u = 1\n")  # untracked
    r = _resolve(git_repo, "staged", include_dirty=True)
    assert r.spec.type == "working-tree"  # widened
    assert "* b * 1" in r.diff  # unstaged work now included
    assert "untracked.py" in r.diff  # untracked work now included


# --- patch ----------------------------------------------------------------------------


def test_patch_scope_uses_provided_text(git_repo, tmp_path):
    patch = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    r = resolve_scope("patch", "ignored.diff", git_repo, patch_text=patch)
    assert r.diff == patch
    assert r.inline_only is True


# --- auto -----------------------------------------------------------------------------


def test_auto_dirty_resolves_working_tree(git_repo):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    r = _resolve(git_repo, "auto")
    assert r.spec.type == "working-tree"


def test_auto_clean_feature_branch_resolves_branch(git_repo):
    git(git_repo, "checkout", "-q", "-b", "feature")
    commit(git_repo, "feat.py", "feat = 1\n", "add feat")
    r = _resolve(git_repo, "auto")
    assert r.spec.type == "branch"


def test_auto_clean_default_branch_is_nothing_to_review(git_repo):
    with pytest.raises(ScopeError):
        _resolve(git_repo, "auto")


# --- conflict detection ---------------------------------------------------------------


def test_in_progress_merge_is_refused(git_repo):
    git(git_repo, "checkout", "-q", "-b", "feature")
    commit(git_repo, "shared.py", "feature_side = 1\n", "feature edit")
    git(git_repo, "checkout", "-q", "main")
    commit(git_repo, "shared.py", "main_side = 1\n", "main edit")
    # Start a merge that conflicts; leave it unresolved.
    res = git_repo  # noqa: F841
    import subprocess

    subprocess.run(["git", "merge", "feature"], cwd=git_repo, capture_output=True)
    with pytest.raises(ScopeError, match="merge"):
        _resolve(git_repo, "working-tree")
    # --allow-conflicts lets it through.
    _resolve(git_repo, "working-tree", allow_conflicts=True)


# --- pr (fake gh) ---------------------------------------------------------------------


def test_pr_scope_uses_gh_diff(git_repo, stub_gh):
    r = _resolve(git_repo, "pr")
    assert "pr_file.py" in r.diff
    assert r.spec.base == "main"


# --- effective-pr (local remote + fetch) ----------------------------------------------


def test_effective_pr_includes_committed_and_dirty(tmp_path):
    import subprocess

    def g(repo, *a):
        return git(repo, *a)

    # Bare remote + working clone.
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", "-q", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    g(work, "config", "user.email", "t@t.com")
    g(work, "config", "user.name", "t")
    g(work, "config", "commit.gpgsign", "false")
    commit(work, "base.py", "base = 1\n", "base")
    g(work, "push", "-q", "origin", "main")
    g(work, "checkout", "-q", "-b", "feature")
    commit(work, "committed.py", "c = 1\n", "committed work")
    (work / "dirty.py").write_text("d = 1\n")  # uncommitted

    r = resolve_scope("effective-pr", None, work)
    assert "committed.py" in r.diff
    assert "dirty.py" in r.diff
    assert "base.py" not in r.diff


# --- adaptive bundling ----------------------------------------------------------------


def test_small_diff_is_inline(git_repo):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    bundle = build_bundle(_resolve(git_repo, "working-tree"))
    assert bundle.mode == "inline"


def test_large_diff_is_self_collect(git_repo):
    # Tracked change (untracked files are capped at 24 KB/file, so they can't blow it).
    commit(git_repo, "big.py", "base\n", "add big")
    big = "x = 1  # padding line to grow the diff\n" * 12000  # well over 256 KB
    (git_repo / "big.py").write_text(big)
    resolved = _resolve(git_repo, "working-tree")
    assert len(resolved.diff.encode("utf-8")) > INLINE_MAX_BYTES
    bundle = build_bundle(resolved)
    assert bundle.mode == "self-collect"
    assert "file(s) changed" in bundle.summary


def test_patch_is_always_inline_even_if_large(git_repo):
    big_patch = "+x\n" * 200000  # > 256 KB
    resolved = resolve_scope("patch", "-", git_repo, patch_text=big_patch)
    assert build_bundle(resolved).mode == "inline"
