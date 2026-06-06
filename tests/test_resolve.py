from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeview.config import HarnessInstance, Settings, ensure_seeded
from aeview.resolve import (
    ResolveError,
    build_roster,
    discover_reviewers,
    resolve_reviewer,
)


def _settings(fallback_model: str = "fallbackmodel") -> Settings:
    return Settings(
        fallback_reviewer_harnesses=[HarnessInstance(harness="claude-code", model=fallback_model)]
    )


def make_reviewer(base: Path, name: str, *, body="BODY", fm_name=None, harnesses=None) -> Path:
    d = base / ".aeview" / "reviewers" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "REVIEWER.md").write_text(
        f"---\nname: {fm_name or name}\ndescription: d\n---\n{body}\n"
    )
    if harnesses is not None:
        (d / "harness.json").write_text(json.dumps({"harnesses": harnesses}))
    return d


# --- walk-up ---------------------------------------------------------------------------


def test_walk_up_first_match_wins(tmp_path):
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    hp = [{"harness": "claude-code", "model": "m"}]
    make_reviewer(repo, "python", body="OUTER", harnesses=hp)
    make_reviewer(sub, "python", body="INNER", harnesses=hp)
    r = resolve_reviewer("python", sub, _settings())
    assert "INNER" in r.body  # nearest rung wins


def test_default_resolves_from_home(aeview_home, tmp_path):
    # ~/.aeview/reviewers/default is seeded; resolve from an unrelated cwd finds it.
    ensure_seeded()
    r = resolve_reviewer("default", tmp_path, _settings())
    assert r.name == "default"
    assert r.source == aeview_home / "reviewers" / "default"


def test_repo_overrides_default(aeview_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "default", body="REPO DEFAULT",
                  harnesses=[{"harness": "claude-code", "model": "m"}])
    r = resolve_reviewer("default", repo, _settings())
    assert "REPO DEFAULT" in r.body
    assert r.source == repo / ".aeview" / "reviewers" / "default"


def test_unknown_reviewer_raises(tmp_path):
    with pytest.raises(ResolveError, match="not found"):
        resolve_reviewer("nope", tmp_path, _settings())


def test_dir_name_must_match_frontmatter(tmp_path):
    make_reviewer(tmp_path, "python", fm_name="pythonista",
                  harnesses=[{"harness": "claude-code", "model": "m"}])
    with pytest.raises(ResolveError, match="does not match"):
        resolve_reviewer("python", tmp_path, _settings())


# --- harness resolution ----------------------------------------------------------------


def test_harness_json_used_when_present(tmp_path):
    make_reviewer(tmp_path, "python", harnesses=[{"harness": "codex", "model": "gpt-5.5"}])
    r = resolve_reviewer("python", tmp_path, _settings())
    assert [h.instance.harness for h in r.harnesses] == ["codex"]
    assert r.harnesses[0].id == "codex-gpt-5.5"


def test_harness_falls_back_to_settings(tmp_path):
    make_reviewer(tmp_path, "python")  # no harness.json
    r = resolve_reviewer("python", tmp_path, _settings("sonnet"))
    assert [h.instance.model for h in r.harnesses] == ["sonnet"]


def test_instance_id_collision_escalates(tmp_path):
    make_reviewer(
        tmp_path, "python",
        harnesses=[
            {"harness": "claude-code", "model": "opus", "thinking": "high"},
            {"harness": "claude-code", "model": "opus", "thinking": "low"},
        ],
    )
    r = resolve_reviewer("python", tmp_path, _settings())
    ids = {h.id for h in r.harnesses}
    assert ids == {"claude-code-opus-high", "claude-code-opus-low"}


# --- discovery & roster ----------------------------------------------------------------


def test_discover_reviewers_includes_default_and_repo(aeview_home, tmp_path):
    ensure_seeded()
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "python", harnesses=[{"harness": "claude-code", "model": "m"}])
    make_reviewer(repo, "security", harnesses=[{"harness": "claude-code", "model": "m"}])
    names = discover_reviewers(repo, _settings())
    assert "default" in names  # from ~/.aeview
    assert "python" in names and "security" in names


def test_build_roster_is_cross_product(tmp_path):
    make_reviewer(
        tmp_path, "python",
        harnesses=[
            {"harness": "claude-code", "model": "opus"},
            {"harness": "codex", "model": "gpt-5.5"},
        ],
    )
    reviewer = resolve_reviewer("python", tmp_path, _settings())
    roster = build_roster([reviewer])
    ids = {e.id for e in roster}
    assert ids == {"python__claude-code-opus", "python__codex-gpt-5.5"}
