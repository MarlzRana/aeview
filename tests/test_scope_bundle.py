from __future__ import annotations

import pytest

from aeview.bundle import build_bundle
from aeview.scope import ScopeError


def test_working_tree_bundle_captures_changes(git_repo):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    bundle = build_bundle("working-tree", git_repo)
    assert not bundle.is_empty
    assert "return a - b" in bundle.diff
    assert bundle.scope.type == "working-tree"
    assert bundle.scope.base == "HEAD"


def test_working_tree_bundle_includes_untracked(git_repo):
    (git_repo / "new.py").write_text("print('hi')\n")
    bundle = build_bundle("working-tree", git_repo)
    assert "new.py" in bundle.diff


def test_clean_tree_is_empty(git_repo):
    bundle = build_bundle("working-tree", git_repo)
    assert bundle.is_empty


def test_unsupported_scope_raises(git_repo):
    with pytest.raises(ScopeError):
        build_bundle("branch", git_repo)
