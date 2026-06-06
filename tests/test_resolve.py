from __future__ import annotations

from pathlib import Path

import pytest

from aeview.config import HarnessInstance, Settings, ensure_seeded
from aeview.harness import get_adapter
from aeview.resolve import (
    ResolveError,
    build_roster,
    discover_reviewers,
    resolve_reviewer,
)
from conftest import make_reviewer

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _settings(fallback_model: str = "fallbackmodel") -> Settings:
    return Settings(
        fallback_reviewer_harnesses=[HarnessInstance(harness="claude-code", model=fallback_model)]
    )


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


def test_reserved_name_all_is_rejected(tmp_path):
    make_reviewer(tmp_path, "all", harnesses=[{"harness": "claude-code", "model": "m"}])
    with pytest.raises(ResolveError, match="reserved"):
        resolve_reviewer("all", tmp_path, _settings())


def test_malformed_yaml_frontmatter_raises_resolve_error(tmp_path):
    d = tmp_path / ".aeview" / "reviewers" / "python"
    d.mkdir(parents=True)
    (d / "REVIEWER.md").write_text("---\nname: [unclosed\n---\nbody\n")  # invalid YAML
    with pytest.raises(ResolveError, match="YAML"):
        resolve_reviewer("python", tmp_path, _settings())


def test_non_mapping_frontmatter_raises_resolve_error(tmp_path):
    d = tmp_path / ".aeview" / "reviewers" / "python"
    d.mkdir(parents=True)
    (d / "REVIEWER.md").write_text("---\n- just a list\n---\nbody\n")
    with pytest.raises(ResolveError, match="mapping"):
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


def test_instance_ids_never_collide_with_escalated_form(tmp_path):
    # A model literally named "opus-high" must not clash with opus + thinking:high.
    make_reviewer(
        tmp_path, "python",
        harnesses=[
            {"harness": "claude-code", "model": "opus-high"},
            {"harness": "claude-code", "model": "opus", "thinking": "high"},
            {"harness": "claude-code", "model": "opus", "thinking": "low"},
        ],
    )
    r = resolve_reviewer("python", tmp_path, _settings())
    ids = [h.id for h in r.harnesses]
    assert len(ids) == len(set(ids))  # all unique -> no file clobbering


def test_malformed_harness_json_raises_resolve_error(tmp_path):
    d = make_reviewer(tmp_path, "python", harnesses=[{"harness": "claude-code", "model": "m"}])
    (d / "harness.json").write_text("{not valid json")
    with pytest.raises(ResolveError, match="invalid"):
        resolve_reviewer("python", tmp_path, _settings())


def test_empty_harness_json_raises(tmp_path):
    make_reviewer(tmp_path, "python", harnesses=[])
    with pytest.raises(ResolveError, match="no harnesses"):
        resolve_reviewer("python", tmp_path, _settings())


def test_discover_first_match_shadows_outer(tmp_path):
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    make_reviewer(repo, "python", harnesses=[{"harness": "claude-code", "model": "m"}])
    make_reviewer(sub, "python", harnesses=[{"harness": "claude-code", "model": "m"}])
    # `python` appears at two rungs but is listed once (nearest shadows the outer).
    assert discover_reviewers(sub).count("python") == 1


# --- discovery & roster ----------------------------------------------------------------


def test_discover_reviewers_includes_default_and_repo(aeview_home, tmp_path):
    ensure_seeded()
    repo = tmp_path / "repo"
    repo.mkdir()
    make_reviewer(repo, "python", harnesses=[{"harness": "claude-code", "model": "m"}])
    make_reviewer(repo, "security", harnesses=[{"harness": "claude-code", "model": "m"}])
    names = discover_reviewers(repo)
    assert "default" in names  # from ~/.aeview
    assert "python" in names and "security" in names


def test_bundled_repo_reviewers_resolve(tmp_path):
    # Guards the repo's own .aeview/reviewers/* configs: each resolves (valid harness.json,
    # dir == frontmatter name) and names a supported harness. Models aren't validated here
    # (they're free-form strings checked only at run time), but a typo'd/broken checked-in
    # config that synthetic tests would miss is caught.
    reviewers_dir = _REPO_ROOT / ".aeview" / "reviewers"
    names = [d.name for d in reviewers_dir.iterdir() if (d / "REVIEWER.md").is_file()]
    assert names, "repo ships no reviewers to validate"
    settings = _settings()
    for name in names:
        reviewer = resolve_reviewer(name, _REPO_ROOT, settings)
        assert reviewer.harnesses
        for ref in reviewer.harnesses:
            get_adapter(ref.instance.harness)  # raises AdapterError on an unsupported harness


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
