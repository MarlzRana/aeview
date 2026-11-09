from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from aeview.bundle import INLINE_MAX_BYTES, build_bundle
from aeview.scope import ScopeError, parse_scope, resolve_base
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
        parse_scope("commits:-x")


def test_parse_scope_allows_stdin_patch_sentinel():
    assert parse_scope("patch:-") == ("patch", "-")


def test_parse_scope_range_requires_a_range():
    # A bare ref would make `git diff <ref>` read the working tree behind the conflict gate.
    with pytest.raises(ScopeError, match="range"):
        parse_scope("range:HEAD")
    assert parse_scope("range:A..B") == ("range", "A..B")
    assert parse_scope("range:A...B") == ("range", "A...B")


def test_parse_scope_commits_list_and_bare():
    # commits is a known type, not value-required: bare passes through (-> HEAD at resolve), and a
    # comma-list is carried verbatim for _parse_commit_refs to split.
    assert parse_scope("commits:a,b,c") == ("commits", "a,b,c")
    assert parse_scope("commits") == ("commits", None)


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


def test_no_head_working_tree_includes_unstaged_edits(tmp_path):
    # Pre-first-commit repo: a file added then modified must still show the unstaged delta.
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "-b", "main", "-q")
    (repo / "f.py").write_text("staged = 1\n")
    git(repo, "add", "f.py")
    (repo / "f.py").write_text("staged = 1\nunstaged = 2\n")  # modify after staging
    r = _resolve(repo, "working-tree")
    assert "unstaged = 2" in r.diff  # the unstaged edit is not dropped


def test_resolve_base_rejects_option_like_ref_segment(git_repo):
    # `origin/-p` slips past parse_scope's guard but its split segment '-p' must not reach
    # `git fetch` as the --prune option.
    with pytest.raises(ScopeError, match="git option"):
        resolve_base(git_repo, "origin/-p", do_fetch=True)


# --- commits / range ------------------------------------------------------------------


def test_commits_default_is_head(git_repo):
    commit(git_repo, "b.py", "b = 1\n", "add b")
    r = _resolve(git_repo, "commits")
    assert "b.py" in r.diff
    assert r.spec.base == "HEAD"


def test_commits_specific_ref(git_repo):
    first = git(git_repo, "rev-parse", "HEAD").strip()
    commit(git_repo, "b.py", "b = 1\n", "add b")
    r = _resolve(git_repo, "commits", first)
    assert "app.py" in r.diff  # the initial commit's content
    assert "b.py" not in r.diff


def test_commits_non_contiguous_set_shows_each_own_patch(git_repo):
    # A set that SKIPS a commit: each listed commit is shown against its own parent, so only the
    # selected commits' added files appear -- the skipped one's addition does not.
    a = commit(git_repo, "a.py", "a = 1\n", "add a")
    commit(git_repo, "b.py", "b = 1\n", "add b")  # skipped
    c = commit(git_repo, "c.py", "c = 1\n", "add c")
    r = _resolve(git_repo, "commits", f"{a},{c}")
    assert "a.py" in r.diff and "c.py" in r.diff
    assert "b.py" not in r.diff
    assert r.spec.base == f"{a},{c}"
    # git show emits in argument order, so requesting c before a flips the emitted diff order --
    # proving the diff itself (not only the base label) honors the user's order.
    rev = _resolve(git_repo, "commits", f"{c},{a}")
    assert rev.diff.index("c.py") < rev.diff.index("a.py")


def test_commits_dedupes_repeated_ref(git_repo):
    sha = commit(git_repo, "b.py", "b = 1\n", "add b")
    r = _resolve(git_repo, "commits", f"{sha},{sha}")
    assert r.spec.base == sha  # collapsed to a single ref, not "<sha>,<sha>"


def test_commits_empty_value_raises(git_repo):
    with pytest.raises(ScopeError, match="at least one commit"):
        _resolve(git_repo, "commits", "")


def test_commits_rejects_range_item(git_repo):
    a = git(git_repo, "rev-parse", "HEAD").strip()
    with pytest.raises(ScopeError, match="range"):
        _resolve(git_repo, "commits", f"{a}..HEAD")


def test_commits_nonexistent_ref_raises(git_repo):
    with pytest.raises(ScopeError, match="does not exist"):
        _resolve(git_repo, "commits", "deadbeefdeadbeef")
    # _ref_exists runs over every ref, so a bad ref in a *later* position is caught too.
    good = git(git_repo, "rev-parse", "HEAD").strip()
    with pytest.raises(ScopeError, match="does not exist"):
        _resolve(git_repo, "commits", f"{good},deadbeefdeadbeef")


def test_commits_accepts_ancestry_refs(git_repo):
    # The leading-'^' rail is leading-only: ancestry refs like HEAD^ are valid single commits that
    # must resolve, not be mistaken for the '^' negation we reject (HEAD^ does not start with '^').
    commit(git_repo, "b.py", "b = 1\n", "add b")  # advance HEAD; HEAD^ is now the initial commit
    r = _resolve(git_repo, "commits", "HEAD^")
    assert "app.py" in r.diff and "b.py" not in r.diff


def test_commits_inspect_is_space_joined(git_repo):
    # Input is comma-separated, but the self-collect `git show` hint must be SPACE-joined
    # (`git show a b`, not `git show a,b` which git reads as one bad ref). Guards that join.
    first = git(git_repo, "rev-parse", "HEAD").strip()
    second = commit(git_repo, "b.py", "b = 1\n", "add b")
    r = _resolve(git_repo, "commits", f"{first},{second}")
    assert r.inspect == [f"git show {first} {second}"]


def test_commits_rejects_option_or_negation_non_first_item(git_repo):
    # The first item is caught by parse_scope's whole-value guard; a *later* item is caught by the
    # per-item rail. '^sha' is a negation that `git show` silently resolves to an empty diff.
    sha = git(git_repo, "rev-parse", "HEAD").strip()
    with pytest.raises(ScopeError, match="may not start with"):
        _resolve(git_repo, "commits", f"{sha},--output=x")
    with pytest.raises(ScopeError, match="may not start with"):
        _resolve(git_repo, "commits", f"{sha},^{sha}")


def test_commits_normalizes_whitespace_and_preserves_order(git_repo):
    # Surrounding/interior whitespace stripped, empty segments dropped, dups removed, and the user's
    # order kept (git show emits in arg order, so the base label records that order, not sorted).
    first = git(git_repo, "rev-parse", "HEAD").strip()
    second = commit(git_repo, "b.py", "b = 1\n", "add b")
    r = _resolve(git_repo, "commits", f"  {second} , {first} ,, {second} ")
    assert r.spec.base == f"{second},{first}"


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


def test_include_dirty_rejected_on_commits(git_repo):
    with pytest.raises(ScopeError):
        _resolve(git_repo, "commits", include_dirty=True)


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


def test_auto_reviews_unpushed_default_branch_commits(tmp_path):
    import subprocess

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", "-q", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    git(work, "config", "user.email", "t@t.com")
    git(work, "config", "user.name", "t")
    git(work, "config", "commit.gpgsign", "false")
    commit(work, "base.py", "base = 1\n", "base")
    git(work, "push", "-q", "origin", "main")
    # New commit on main, not pushed -> HEAD ahead of origin/main, clean tree.
    commit(work, "extra.py", "extra = 1\n", "unpushed work")

    r = _resolve(work, "auto")
    assert r.spec.type == "branch"
    assert "extra.py" in r.diff
    assert "base.py" not in r.diff


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


def test_in_progress_merge_does_not_block_historical_scopes(git_repo):
    # commits/range diff historical refs and don't read the working tree, so an in-progress
    # merge must not block them.
    first = git(git_repo, "rev-parse", "HEAD").strip()
    git(git_repo, "checkout", "-q", "-b", "feature")
    commit(git_repo, "shared.py", "feature_side = 1\n", "feature edit")
    git(git_repo, "checkout", "-q", "main")
    second = commit(git_repo, "shared.py", "main_side = 1\n", "main edit")
    import subprocess

    subprocess.run(["git", "merge", "feature"], cwd=git_repo, capture_output=True)  # conflicts
    # These resolve despite the unfinished merge.
    assert not _resolve(git_repo, "commits", first).is_empty
    assert not _resolve(git_repo, "range", f"{first}..{second}").is_empty


# --- pr (fake gh) ---------------------------------------------------------------------


def test_pr_scope_uses_gh_diff(git_repo, stub_gh):
    r = _resolve(git_repo, "pr")
    assert "pr_file.py" in r.diff
    assert r.spec.base == "main"
    # No head SHA fetched unless the caller will post (capture_head); a plain review shouldn't pay.
    assert r.head_sha is None


def test_pr_scope_captures_head_for_posting(git_repo, stub_gh):
    # With capture_head (set when --post-comments), the head the diff was taken against is pinned so
    # posted comments anchor to it. The stub reports a stable head before + after the diff.
    r = _resolve(git_repo, "pr", capture_head=True)
    assert r.head_sha == "deadbeefcafe"


def test_pr_scope_head_moved_midfetch_yields_no_head_sha(git_repo, stub_gh, monkeypatch):
    # If the PR head differs before vs after the diff fetch, we can't prove which commit the diff
    # matches -> head_sha is None (posting then drops to a summary-only comment).
    from aeview import scope

    heads = iter(["sha_before", "sha_after"])
    monkeypatch.setattr(scope, "_pr_head", lambda cwd, value: next(heads))
    r = _resolve(git_repo, "pr", capture_head=True)
    assert r.head_sha is None


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


# --- pr: waiting for GitHub to catch up with a just-pushed head -------------------------


@pytest.fixture
def pushed_pr(git_repo, tmp_path, monkeypatch):
    """`feature` checked out, tracking a bare remote it was pushed to, as PR #7 with GitHub faked:
    headRefOid reports `state["stale"]` (default: the previous push) for `lag` reads, then the
    pushed tip (`@{upstream}`)."""
    from aeview import scope

    remote = tmp_path / "remote.git"
    git(git_repo, "init", "-q", "--bare", str(remote))
    git(git_repo, "remote", "add", "origin", str(remote))
    git(git_repo, "checkout", "-q", "-b", "feature")
    old = commit(git_repo, "f.py", "v = 1\n", "first")
    local = commit(git_repo, "f.py", "v = 2\n", "second")
    git(git_repo, "push", "-q", "-u", "origin", "feature")

    state: dict = {"lag": 0, "reads": 0, "stale": old, "number": 7, "current": 7, "gh_ok": True}
    state["branch"] = "feature"

    def head(cwd, value, timeout=30.0):
        state["reads"] += 1
        if state.get("stall"):
            time.sleep(timeout)  # a stalled gh, bounded by the timeout it was given
        if state["reads"] <= state["lag"]:
            return state["stale"]
        return git(git_repo, "rev-parse", "refs/remotes/origin/feature").strip()

    def fields(cwd, value, names, timeout=30.0):
        if not state["gh_ok"]:
            return None
        if names == ["number"]:  # the checked-out branch's PR
            return {"number": state["current"]}
        data: dict[str, object] = {
            "number": state["number"],
            "headRefName": state["branch"],
            "baseRefName": "main",
        }
        if "headRefOid" in names:
            data["headRefOid"] = head(cwd, value, timeout)
        return data

    monkeypatch.setattr(scope, "_pr_head", head)
    monkeypatch.setattr(scope, "_pr_view_fields", fields)
    monkeypatch.setattr(scope, "_PUSH_SETTLE_INTERVAL", 0.01)
    monkeypatch.setattr(scope, "_PUSH_SETTLE_TIMEOUT", 0.3)
    return SimpleNamespace(repo=git_repo, old=old, local=local, state=state)


def _head(repo) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def test_pr_scope_waits_for_github_to_catch_up_with_the_push(pushed_pr, stub_gh):
    pushed_pr.state["lag"] = 3  # GitHub still reports the previous head for a few reads
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert r.head_sha == pushed_pr.local  # pinned to the pushed commit, not summary-only/stale
    assert r.notice is None


def test_pr_scope_already_caught_up_pins_without_polling(pushed_pr, stub_gh):
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert r.head_sha == pushed_pr.local
    assert r.notice is None
    assert pushed_pr.state["reads"] == 3  # settle read + the two bracket reads


@pytest.mark.parametrize("rewrite", ["amend", "rollback"])
def test_pr_scope_waits_after_a_force_push(pushed_pr, stub_gh, rewrite):
    # The stale head isn't an ancestor of the new tip (amend) or is ahead of it (rollback), but our
    # clone has it either way: it's our own lagging push.
    if rewrite == "amend":
        git(pushed_pr.repo, "commit", "-q", "--amend", "-m", "second, amended")
    else:
        git(pushed_pr.repo, "reset", "-q", "--hard", "HEAD~1")
    git(pushed_pr.repo, "push", "-q", "--force", "origin", "feature")
    pushed_pr.state["stale"] = pushed_pr.local
    pushed_pr.state["lag"] = 3
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert r.head_sha == _head(pushed_pr.repo)
    assert r.notice is None


def test_pr_scope_lagging_past_the_wait_says_so(pushed_pr, stub_gh):
    pushed_pr.state["lag"] = 10_000  # never catches up within the (shortened) wait
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert r.head_sha == pushed_pr.old  # consistent with the diff GitHub served
    assert r.notice is not None
    assert "hasn't caught up" in r.notice


def test_pr_scope_stalled_gh_cannot_outlast_the_wait(pushed_pr, stub_gh, monkeypatch):
    from aeview import scope

    pushed_pr.state["lag"] = 10_000
    real_head = scope._pr_head
    calls = {"n": 0}

    def stall_on_first_poll(cwd, value, timeout=30.0):
        calls["n"] += 1
        pushed_pr.state["stall"] = calls["n"] == 1  # this poll stalls for its whole budget
        return real_head(cwd, value, timeout)

    monkeypatch.setattr(scope, "_pr_head", stall_on_first_poll)
    started = time.monotonic()
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert time.monotonic() - started < 1.0  # bounded by the 0.3s wait, not gh's 30s timeout
    assert r.notice is not None


def test_pr_scope_unpushed_wip_still_waits_for_the_push(pushed_pr, stub_gh):
    # Push, then commit more locally: GitHub converges on the pushed tip, not HEAD.
    commit(pushed_pr.repo, "f.py", "v = 3\n", "unpushed")
    pushed_pr.state["lag"] = 3
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert r.head_sha == pushed_pr.local  # the pushed tip
    assert r.notice is not None
    assert "1 commit(s) not pushed" in r.notice


def test_pr_scope_unpushed_commits_warn_without_waiting(pushed_pr, stub_gh):
    commit(pushed_pr.repo, "f.py", "v = 3\n", "unpushed")
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert r.notice is not None
    assert "1 commit(s) not pushed" in r.notice
    assert pushed_pr.state["reads"] == 3  # GitHub already at the pushed tip: no polling


def test_pr_scope_local_behind_upstream_neither_waits_nor_warns(pushed_pr, stub_gh):
    git(pushed_pr.repo, "reset", "-q", "--hard", "HEAD~1")  # upstream (the PR) is ahead of us
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert r.notice is None
    assert pushed_pr.state["reads"] == 3


def test_pr_scope_head_pushed_elsewhere_skips_the_wait(pushed_pr, stub_gh):
    # A collaborator's push / GitHub's "Update branch" isn't in our clone and will never become our
    # upstream: don't wait the full timeout; say what's being reviewed.
    pushed_pr.state["stale"] = "0" * 40
    pushed_pr.state["lag"] = 10_000
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert r.notice is not None
    assert "the PR has commits your local branch doesn't" in r.notice
    assert pushed_pr.state["reads"] == 3
    assert r.head_sha == pushed_pr.state["stale"]  # the head GitHub's diff was computed against


def test_pr_scope_other_pr_with_a_same_named_branch_skips_the_wait(pushed_pr, stub_gh):
    # `--scope pr:<n>` for a PR that isn't the checked-out branch's (e.g. a fork's `feature`).
    pushed_pr.state["number"] = 99
    pushed_pr.state["lag"] = 10_000
    r = _resolve(pushed_pr.repo, "pr", "99", capture_head=True)
    assert r.notice is None
    assert pushed_pr.state["reads"] == 3


def test_pr_scope_explicit_number_of_the_current_branch_waits(pushed_pr, stub_gh):
    pushed_pr.state["lag"] = 3
    assert _resolve(pushed_pr.repo, "pr", "7", capture_head=True).head_sha == pushed_pr.local


def test_pr_scope_head_branch_without_a_tracking_ref_skips_the_wait(pushed_pr, stub_gh):
    pushed_pr.state["branch"] = "never-fetched"
    pushed_pr.state["lag"] = 10_000
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert r.notice is None
    assert pushed_pr.state["reads"] == 3


def test_pr_scope_branch_tracking_another_ref_still_waits_on_the_pr_branch(pushed_pr, stub_gh):
    # `checkout -b feat origin/main` + `push origin feat`: the local branch tracks origin/main, but
    # the push still updated origin/feature — that's the tip to wait for, and nothing is unpushed.
    git(pushed_pr.repo, "push", "-q", "origin", "main")
    git(pushed_pr.repo, "branch", "--set-upstream-to=origin/main")
    pushed_pr.state["lag"] = 3
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert r.head_sha == pushed_pr.local
    assert r.notice is None


def test_pr_scope_never_caught_up_with_unpushed_wip_reports_both(pushed_pr, stub_gh):
    commit(pushed_pr.repo, "f.py", "v = 3\n", "unpushed")
    pushed_pr.state["lag"] = 10_000
    r = _resolve(pushed_pr.repo, "pr", capture_head=True)
    assert r.notice == (
        "GitHub hasn't caught up with your push after 0s; "
        "local HEAD has 1 commit(s) not pushed to the PR; "
        "reviewing the PR as it is on GitHub"
    )


def test_pushed_tip_ref_finds_the_one_remote_carrying_the_branch(git_repo):
    from aeview import scope

    head = git(git_repo, "rev-parse", "HEAD").strip()
    git(git_repo, "update-ref", "refs/remotes/fork/fix/thing", head)
    git(git_repo, "update-ref", "refs/remotes/upstream/main", head)
    assert scope._pushed_tip_ref(git_repo, "fix/thing") == "refs/remotes/fork/fix/thing"
    assert scope._pushed_tip_ref(git_repo, "thing") is None  # no partial-segment match
    git(git_repo, "update-ref", "refs/remotes/origin/fix/thing", head)
    assert scope._pushed_tip_ref(git_repo, "fix/thing") is None  # ambiguous: two remotes


def test_pr_scope_gh_view_failure_skips_the_wait(pushed_pr, stub_gh):
    pushed_pr.state["gh_ok"] = False
    assert _resolve(pushed_pr.repo, "pr", capture_head=True).notice is None


def test_pr_scope_without_capture_never_waits(pushed_pr, stub_gh):
    pushed_pr.state["lag"] = 10_000
    _resolve(pushed_pr.repo, "pr")
    assert pushed_pr.state["reads"] == 0


def test_pr_head_forwards_its_timeout_to_gh(monkeypatch, tmp_path):
    from aeview import scope
    from aeview.process import ProcResult

    seen: list[float | None] = []

    def fake_run_sync(args, cwd=None, timeout=None):
        seen.append(timeout)
        return ProcResult(0, '{"headRefOid": "abc"}', "")

    monkeypatch.setattr(scope, "run_sync", fake_run_sync)
    assert scope._pr_head(tmp_path, None, timeout=1.5) == "abc"
    assert seen == [1.5]


def test_pr_view_fields_requests_and_parses_every_field(monkeypatch, tmp_path):
    from aeview import scope
    from aeview.process import ProcResult

    seen: list[list[str]] = []

    def fake_run_sync(args, cwd=None, timeout=None):
        seen.append(args)
        return ProcResult(0, '{"number": 7, "headRefOid": "abc"}', "")

    monkeypatch.setattr(scope, "run_sync", fake_run_sync)
    data = scope._pr_view_fields(tmp_path, "7", ["number", "headRefOid"])
    assert data == {"number": 7, "headRefOid": "abc"}
    assert seen == [["gh", "pr", "view", "7", "--json", "number,headRefOid"]]


@pytest.mark.parametrize("stdout", ["[]", '{"headRefOid": 5}', "not json"])
def test_pr_head_rejects_malformed_gh_output(monkeypatch, tmp_path, stdout):
    from aeview import scope
    from aeview.process import ProcResult

    monkeypatch.setattr(scope, "run_sync", lambda *a, **k: ProcResult(0, stdout, ""))
    assert scope._pr_head(tmp_path, None) is None
