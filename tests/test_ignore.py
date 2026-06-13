from __future__ import annotations

from pathlib import Path

from pathspec import GitIgnoreSpec

from aeview.ignore import filter_diff, filter_resolved
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
