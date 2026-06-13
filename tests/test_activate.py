from __future__ import annotations

import pytest

from aeview.activate import select_auto_reviewers
from conftest import make_reviewer

HARNESS = [{"harness": "claude-code", "model": "opus"}]


def _diff(*paths: str) -> str:
    """Minimal unified-diff blocks (one modified hunk) for the given b/ paths."""
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new\n" for p in paths
    )


def test_activates_on_matching_glob(aeview_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "backend", harnesses=HARNESS, auto_activate_paths=["backend/**"])
    diff = _diff("backend/api.py", "frontend/app.js")
    assert select_auto_reviewers(repo, repo, diff) == ["backend"]


def test_no_match_does_not_activate(aeview_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "backend", harnesses=HARNESS, auto_activate_paths=["backend/**"])
    assert select_auto_reviewers(repo, repo, _diff("frontend/app.js")) == []


def test_reviewer_without_paths_never_activates(aeview_home, tmp_path):
    # A reviewer with no auto-activate-paths only runs by name / `all`, never auto.
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "plain", harnesses=HARNESS)  # no auto_activate_paths key
    assert select_auto_reviewers(repo, repo, _diff("plain/whatever.py")) == []


@pytest.mark.parametrize(
    ("glob", "changed", "matches"),
    [
        # The literal-glob (PurePath.full_match) table the user selected over the gitignore engine.
        ("backend/**", "backend/api/routes.py", True),  # ** crosses directories
        ("backend/", "backend/api/routes.py", False),  # bare dir matches nothing -> use dir/**
        ("*.py", "backend/api/routes.py", False),  # single * never crosses a slash
        ("*.py", "routes.py", True),  # ... but matches a root-level file
        ("backend/*.py", "backend/api/routes.py", False),  # one * = one segment under backend/
        ("backend/*.py", "backend/routes.py", True),
        ("**/*.py", "backend/api/routes.py", True),  # ** + * reaches any depth
    ],
)
def test_literal_glob_semantics(aeview_home, tmp_path, glob, changed, matches):
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "r", harnesses=HARNESS, auto_activate_paths=[glob])
    activated = select_auto_reviewers(repo, repo, _diff(changed))
    assert activated == (["r"] if matches else [])


def test_matching_is_case_sensitive(aeview_home, tmp_path):
    # Deterministic across macOS/Linux: a lowercase glob never matches an uppercase path.
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "r", harnesses=HARNESS, auto_activate_paths=["*.py"])
    assert select_auto_reviewers(repo, repo, _diff("MODULE.PY")) == []


def test_any_glob_any_file_activates(aeview_home, tmp_path):
    # A reviewer activates if ANY changed file matches ANY of its globs.
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "r", harnesses=HARNESS, auto_activate_paths=["docs/**", "*.toml"])
    assert select_auto_reviewers(repo, repo, _diff("src/app.py", "pyproject.toml")) == ["r"]


def test_broken_frontmatter_is_skipped(aeview_home, tmp_path):
    # An unparseable reviewer can't declare globs, so it's silently not auto-activated — and it
    # must not crash the selection of a valid sibling that does match.
    repo = tmp_path / "repo"
    repo.mkdir()
    broken = repo / ".aeview" / "reviewers" / "broken"
    broken.mkdir(parents=True)
    (broken / "REVIEWER.md").write_text("---\nname: broken\nbogus_key: 1\n---\nbody\n")
    make_reviewer(repo, "good", harnesses=HARNESS, auto_activate_paths=["**/*.py"])
    assert select_auto_reviewers(repo, repo, _diff("x.py")) == ["good"]


def test_home_reviewer_matches_repo_under_home(aeview_home, tmp_path):
    # No clamp: a home reviewer anchored at ~ reaches a repo under home via a **/ glob. Its globs
    # are matched relative to ~, so the path includes the repo dir segment.
    home = aeview_home.parent  # ~  (aeview_home is ~/.aeview)
    make_reviewer(home, "global", harnesses=HARNESS, auto_activate_paths=["**/*.py"])
    repo = home / "proj"
    repo.mkdir()
    assert "global" in select_auto_reviewers(repo, repo, _diff("x.py"))


def test_multiple_reviewers_activate_in_discovery_order(aeview_home, tmp_path):
    # All matching reviewers activate, nearest-first (here: sorted within the one rung).
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "alpha", harnesses=HARNESS, auto_activate_paths=["*.py"])
    make_reviewer(repo, "beta", harnesses=HARNESS, auto_activate_paths=["*.py"])
    assert select_auto_reviewers(repo, repo, _diff("x.py")) == ["alpha", "beta"]


def test_home_reviewer_does_not_match_repo_outside_home(aeview_home, tmp_path):
    # The scope-path under-check: a repo NOT under ~ has no file the home reviewer can claim.
    home = aeview_home.parent
    make_reviewer(home, "global", harnesses=HARNESS, auto_activate_paths=["**/*.py"])
    outside = tmp_path / "outside"  # sibling of ~, not beneath it
    outside.mkdir()
    assert select_auto_reviewers(outside, outside, _diff("x.py")) == []


def test_home_and_repo_reviewers_both_activate(aeview_home, tmp_path):
    # Multi-rung: a home reviewer (anchored at ~, needs **/) and a repo reviewer both match the same
    # change. Nearest-first order puts the repo reviewer ahead of the home one.
    home = aeview_home.parent
    make_reviewer(home, "global", harnesses=HARNESS, auto_activate_paths=["**/*.py"])
    repo = home / "proj"
    repo.mkdir()
    make_reviewer(repo, "local", harnesses=HARNESS, auto_activate_paths=["*.py"])
    assert select_auto_reviewers(repo, repo, _diff("x.py")) == ["local", "global"]


def test_shadowing_uses_nearest_reviewers_globs(aeview_home, tmp_path):
    # A repo reviewer shadows a same-name home reviewer; the nearest (repo) one's globs decide
    # activation. Home's globs would match, the repo's don't -> no activation.
    home = aeview_home.parent
    make_reviewer(home, "dup", harnesses=HARNESS, auto_activate_paths=["**/*.py"])  # would match
    repo = home / "proj"
    repo.mkdir()
    make_reviewer(repo, "dup", harnesses=HARNESS, auto_activate_paths=["nomatch/**"])  # shadows
    assert select_auto_reviewers(repo, repo, _diff("x.py")) == []


def test_bang_glob_is_literal_not_negation(aeview_home, tmp_path):
    # Deliberate divergence from .aeviewignore's gitignore engine: `!` has no special meaning, so
    # `!*.py` is literal, never a re-include — the `*.py` match stands.
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "r", harnesses=HARNESS, auto_activate_paths=["*.py", "!*.py"])
    assert select_auto_reviewers(repo, repo, _diff("app.py")) == ["r"]


def test_malformed_glob_does_not_crash(aeview_home, tmp_path):
    # On the supported runtime (Python >=3.14) PurePath.full_match returns False for a malformed
    # pattern rather than raising, so a typo'd glob just never matches — it must not abort the run.
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "r", harnesses=HARNESS, auto_activate_paths=["[", "*.py"])
    assert select_auto_reviewers(repo, repo, _diff("app.py")) == ["r"]  # *.py still wins
    assert select_auto_reviewers(repo, repo, _diff("data.json")) == []  # bad glob alone -> no crash
