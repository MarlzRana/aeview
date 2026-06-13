from __future__ import annotations

from pathlib import Path

from pathspec import GitIgnoreSpec

from aeview.ignore import _is_ignored, _load_specs, filter_diff, filter_resolved
from aeview.schema import ScopeSpec
from aeview.scope import ResolvedScope


def _diff(*paths: str) -> str:
    """Minimal unified-diff blocks (one modified hunk) for the given b/ paths."""
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new\n" for p in paths
    )


def _specs(*pairs: tuple[Path, list[str]]) -> list[tuple[Path, GitIgnoreSpec]]:
    return [(root, GitIgnoreSpec.from_lines(lines)) for root, lines in pairs]


# --- filter_diff (pure) ----------------------------------------------------------------


def test_filter_diff_drops_matching(tmp_path):
    diff = _diff("uv.lock", "src/app.py")
    out, ignored = filter_diff(diff, tmp_path, _specs((tmp_path, ["*.lock"])))
    assert ignored == ["uv.lock"]
    assert "b/uv.lock" not in out
    assert "b/src/app.py" in out


def test_filter_diff_negation_reincludes(tmp_path):
    out, ignored = filter_diff(
        _diff("uv.lock", "keep.lock"), tmp_path, _specs((tmp_path, ["*.lock", "!keep.lock"]))
    )
    assert ignored == ["uv.lock"]
    assert "b/keep.lock" in out


def test_filter_diff_no_match_is_unchanged(tmp_path):
    diff = _diff("src/app.py", "README.md")
    out, ignored = filter_diff(diff, tmp_path, _specs((tmp_path, ["*.lock"])))
    assert ignored == []
    assert out == diff


def test_filter_diff_preserves_preamble(tmp_path):
    # `git show` prepends a commit header before the first `diff --git`; it must survive.
    diff = "commit abc123\nAuthor: x\n\n    a message\n\n" + _diff("uv.lock", "a.py")
    out, ignored = filter_diff(diff, tmp_path, _specs((tmp_path, ["*.lock"])))
    assert out.startswith("commit abc123")
    assert "b/a.py" in out and "b/uv.lock" not in out


def test_filter_diff_deletion_uses_old_path(tmp_path):
    block = (
        "diff --git a/old.lock b/old.lock\ndeleted file mode 100644\n"
        "--- a/old.lock\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    out, ignored = filter_diff(block, tmp_path, _specs((tmp_path, ["*.lock"])))
    assert ignored == ["old.lock"]
    assert out == ""


def test_nearer_rung_negation_overrides_farther(tmp_path):
    # Faithful cross-file precedence: the rung nearest the file wins. The farther rung ignores
    # *.lock; the nearer one re-includes keep.lock via negation.
    repo = tmp_path / "repo"
    repo.mkdir()
    specs = _specs((repo, ["!keep.lock"]), (tmp_path, ["*.lock"]))  # nearest-first
    out, ignored = filter_diff(_diff("keep.lock", "x.lock"), repo, specs)
    assert ignored == ["x.lock"]  # x.lock ignored by the farther rung
    assert "b/keep.lock" in out  # re-included by the nearer rung


def test_spoofed_content_header_does_not_redirect_match(tmp_path):
    # A diff *content* line that looks like `+++ b/<ignored>` (i.e. an added line whose text starts
    # with `++ b/...`) must not hijack the block's path and hide a real file from review.
    block = (
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        "@@ -1 +1,2 @@\n app\n+++ b/uv.lock\n"
    )
    out, ignored = filter_diff(block, tmp_path, _specs((tmp_path, ["*.lock"])))
    assert ignored == []  # app.py is the real path; the `+++ b/uv.lock` is spoofed content
    assert "b/app.py" in out


def test_rename_from_ignored_path_is_dropped(tmp_path):
    # Renaming an ignored file to an allowed path must drop the block — its content (the rename
    # delta) would otherwise leak the ignored file.
    block = (
        "diff --git a/secret.lock b/public.txt\nsimilarity index 80%\n"
        "rename from secret.lock\nrename to public.txt\n"
        "--- a/secret.lock\n+++ b/public.txt\n@@ -1 +1 @@\n-x\n+y\n"
    )
    out, ignored = filter_diff(block, tmp_path, _specs((tmp_path, ["*.lock"])))
    assert ignored == ["secret.lock"]
    assert out == ""


def test_copy_from_ignored_path_is_dropped(tmp_path):
    # Symmetric to renames: a copy from an ignored path also leaks the ignored content -> drop.
    block = (
        "diff --git a/secret.lock b/public.txt\nsimilarity index 80%\n"
        "copy from secret.lock\ncopy to public.txt\n"
        "--- a/secret.lock\n+++ b/public.txt\n@@ -1 +1 @@\n-x\n+y\n"
    )
    out, ignored = filter_diff(block, tmp_path, _specs((tmp_path, ["*.lock"])))
    assert ignored == ["secret.lock"] and out == ""


def test_all_ignored_drops_preamble(tmp_path):
    # Every block ignored -> the preamble (e.g. git show's commit header) is dropped too, so the
    # result is genuinely empty and "nothing to review" can fire for an all-ignored commit.
    diff = "commit abc123\nAuthor: x\n\n    a message\n\n" + _diff("uv.lock")
    out, ignored = filter_diff(diff, tmp_path, _specs((tmp_path, ["*.lock"])))
    assert out == "" and ignored == ["uv.lock"]


def test_rename_only_and_mode_only_blocks_match_by_header(tmp_path):
    # Blocks with no `---`/`+++` (pure rename or mode change) resolve their path from the header.
    rename_only = (
        "diff --git a/old.lock b/new.lock\nsimilarity index 100%\n"
        "rename from old.lock\nrename to new.lock\n"
    )
    out, ignored = filter_diff(rename_only, tmp_path, _specs((tmp_path, ["*.lock"])))
    assert ignored == ["new.lock", "old.lock"] and out == ""
    mode_only = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
    out, ignored = filter_diff(mode_only, tmp_path, _specs((tmp_path, ["*.sh"])))
    assert ignored == ["run.sh"] and out == ""


# --- filter_resolved (integration with a real repo) ------------------------------------


def _resolved(diff: str, stype: str = "working-tree") -> ResolvedScope:
    return ResolvedScope(spec=ScopeSpec(type=stype, base="HEAD"), diff=diff, summary="s")


def test_filter_resolved_drops_ignored_in_repo(aeview_home, git_repo):
    (git_repo / ".aeviewignore").write_text("*.lock\n")
    new, ignored = filter_resolved(_resolved(_diff("uv.lock", "app.py")), git_repo)
    assert ignored == ["uv.lock"]
    assert "b/app.py" in new.diff and "b/uv.lock" not in new.diff


def test_filter_resolved_no_ignore_file_is_noop(aeview_home, git_repo):
    resolved = _resolved(_diff("uv.lock", "app.py"))
    new, ignored = filter_resolved(resolved, git_repo)
    assert ignored == []
    assert new is resolved  # unchanged object — no rebuild when nothing matched


def test_filter_resolved_skips_patch_scope(aeview_home, git_repo):
    (git_repo / ".aeviewignore").write_text("*.lock\n")
    resolved = _resolved(_diff("uv.lock"), stype="patch")
    new, ignored = filter_resolved(resolved, git_repo)
    assert ignored == []  # patch paths aren't repo-root-relative; left untouched
    assert new is resolved


def test_filter_resolved_rebuilds_summary(aeview_home, git_repo):
    (git_repo / ".aeviewignore").write_text("*.lock\n")
    new, _ = filter_resolved(_resolved(_diff("uv.lock", "app.py")), git_repo)
    assert "app.py" in new.summary and "uv.lock" not in new.summary  # summary tracks the kept diff


def test_filter_resolved_non_git_cwd_is_noop(aeview_home, tmp_path):
    # A .aeviewignore exists but cwd is not a git repo: `git rev-parse` fails, so we can't anchor
    # the repo-relative diff paths -> return the scope untouched rather than mis-resolve.
    (tmp_path / ".aeviewignore").write_text("*.lock\n")
    resolved = _resolved(_diff("uv.lock"))
    new, ignored = filter_resolved(resolved, tmp_path)
    assert ignored == [] and new is resolved


def test_load_specs_walks_rungs_and_composes(aeview_home, tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / ".aeviewignore").write_text("*.lock\n")
    (inner / ".aeviewignore").write_text("!keep.lock\n")
    specs = _load_specs(inner)
    roots = [r for r, _ in specs]
    assert roots[0] == inner and outer in roots  # nearest-first, both real files loaded
    assert _is_ignored(inner / "x.lock", specs) is True  # outer ignores *.lock
    assert _is_ignored(inner / "keep.lock", specs) is False  # nearer rung's negation wins


def test_load_specs_skips_unreadable(aeview_home, tmp_path):
    (tmp_path / ".aeviewignore").mkdir()  # a directory by that name -> unreadable as text
    specs = _load_specs(tmp_path)  # must not raise
    assert all(root != tmp_path for root, _ in specs)  # the unreadable one was skipped


def test_subdir_cwd_anchors_against_repo_root(aeview_home, git_repo):
    # Run from a subdir: a repo-root .aeviewignore still matches a repo-root file (diff paths are
    # repo-root-relative), and a subdir .aeviewignore does NOT reach files above it.
    sub = git_repo / "src"
    sub.mkdir()
    (git_repo / ".aeviewignore").write_text("*.lock\n")
    (sub / ".aeviewignore").write_text("local.txt\n")
    new, ignored = filter_resolved(_resolved(_diff("uv.lock", "local.txt")), sub)
    assert ignored == ["uv.lock"]  # repo-root rule matches the repo-root file from a subdir cwd
    assert "b/local.txt" in new.diff  # the subdir rule (local.txt) doesn't match repo/local.txt
