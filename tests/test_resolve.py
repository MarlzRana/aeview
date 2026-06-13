from __future__ import annotations

from pathlib import Path

import pytest

from aeview.config import HarnessInstance, Settings, ensure_seeded
from aeview.harness import get_adapter
from aeview.resolve import (
    ResolveError,
    build_roster,
    discover_reviewers,
    parse_reviewer,
    resolve_reviewer,
)
from conftest import make_reviewer

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _settings(fallback_model: str = "fallbackmodel") -> Settings:
    return Settings(
        fallback_reviewer_harnesses=[HarnessInstance(harness="claude-code", model=fallback_model)]
    )


def _write_reviewer_md(base: Path, name: str, frontmatter: str) -> Path:
    """Write a reviewer with raw frontmatter (for shapes make_reviewer can't express)."""
    d = base / ".aeview" / "reviewers" / name
    d.mkdir(parents=True)
    path = d / "REVIEWER.md"
    path.write_text(f"---\n{frontmatter}\n---\nbody\n")
    return path


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
    # The seeded default's harnesses come from its frontmatter, not the fallback — assert the
    # resolved instance so a malformed/dropped seeded harnesses block is caught, not masked.
    assert [(h.instance.harness, h.instance.model) for h in r.harnesses] == [
        ("claude-code", "claude-opus-4-8")
    ]


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


def test_no_frontmatter_raises_resolve_error(tmp_path):
    # parse_reviewer now routes through split_frontmatter and raises on front-is-None; a file
    # with no opening `---` must be rejected (not silently treated as an empty-meta body).
    d = tmp_path / ".aeview" / "reviewers" / "python"
    d.mkdir(parents=True)
    (d / "REVIEWER.md").write_text("just a body, no frontmatter at all\n")
    with pytest.raises(ResolveError, match="missing or has malformed"):
        resolve_reviewer("python", tmp_path, _settings())


def test_unterminated_frontmatter_raises_resolve_error(tmp_path):
    # Opens `---` but never closes it -> split_frontmatter returns front=None -> rejected.
    d = tmp_path / ".aeview" / "reviewers" / "python"
    d.mkdir(parents=True)
    (d / "REVIEWER.md").write_text("---\nname: python\nno closing fence\n")
    with pytest.raises(ResolveError, match="missing or has malformed"):
        resolve_reviewer("python", tmp_path, _settings())


# --- harness resolution ----------------------------------------------------------------


def test_frontmatter_harnesses_used_when_present(tmp_path):
    make_reviewer(tmp_path, "python", harnesses=[{"harness": "codex", "model": "gpt-5.5"}])
    r = resolve_reviewer("python", tmp_path, _settings())
    assert [h.instance.harness for h in r.harnesses] == ["codex"]
    assert r.harnesses[0].id == "codex-gpt-5.5"


def test_harness_falls_back_to_settings(tmp_path):
    make_reviewer(tmp_path, "python")  # no harnesses: block in frontmatter
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


def test_malformed_frontmatter_harnesses_raises_resolve_error(tmp_path):
    # A harness entry missing a required field (model) fails frontmatter validation.
    make_reviewer(tmp_path, "python", harnesses=[{"harness": "codex"}])
    with pytest.raises(ResolveError, match="invalid frontmatter"):
        resolve_reviewer("python", tmp_path, _settings())


def test_unknown_frontmatter_key_raises_resolve_error(tmp_path):
    # extra="forbid" turns a typo'd key into a clear error instead of silently ignoring it.
    _write_reviewer_md(tmp_path, "python", "name: python\ndescription: d\nharneses: []")
    with pytest.raises(ResolveError, match="invalid frontmatter"):
        resolve_reviewer("python", tmp_path, _settings())


def test_empty_harnesses_block_raises(tmp_path):
    make_reviewer(tmp_path, "python", harnesses=[])  # explicit `harnesses: []`
    with pytest.raises(ResolveError, match="present but empty"):
        resolve_reviewer("python", tmp_path, _settings())


def test_blank_harnesses_block_raises(tmp_path):
    # A present-but-null `harnesses:` is a likely mistake; it must error, not select the fallback.
    _write_reviewer_md(tmp_path, "python", "name: python\ndescription: d\nharnesses:")
    with pytest.raises(ResolveError, match="present but empty"):
        resolve_reviewer("python", tmp_path, _settings())


def test_omitted_harnesses_with_empty_fallback_raises(tmp_path):
    # No frontmatter harnesses AND no global fallback -> a clear error, never an empty roster.
    make_reviewer(tmp_path, "python")  # no harnesses: block
    with pytest.raises(ResolveError, match="fallbackReviewerHarnesses is empty"):
        resolve_reviewer("python", tmp_path, Settings(fallback_reviewer_harnesses=[]))


def test_auto_activate_paths_parsed_from_frontmatter(tmp_path):
    # Parsed + validated now; nothing consumes it yet (a later increment adds the path-matching).
    # Pins the kebab-case alias so a regression there is caught.
    path = _write_reviewer_md(
        tmp_path, "py",
        "name: py\ndescription: d\n"
        'harnesses: [{"harness": "codex", "model": "gpt-5.5"}]\n'
        'auto-activate-paths: ["src/**", "docs/*.md"]',
    )
    front, _ = parse_reviewer(path)
    assert front.auto_activate_paths == ["src/**", "docs/*.md"]


def test_underscore_auto_activate_paths_key_rejected(tmp_path):
    # populate_by_name is off, so only the kebab `auto-activate-paths` is accepted; the underscore
    # spelling is an unknown key under extra="forbid". Pins the "one spelling per key" contract.
    _write_reviewer_md(
        tmp_path, "py",
        "name: py\ndescription: d\n"
        'harnesses: [{"harness": "codex", "model": "gpt-5.5"}]\n'
        'auto_activate_paths: ["src/**"]',
    )
    with pytest.raises(ResolveError, match="invalid frontmatter"):
        resolve_reviewer("py", tmp_path, _settings())


@pytest.mark.parametrize("frontmatter", ['name: ""\ndescription: d', "description: d"])
def test_invalid_name_raises_resolve_error(tmp_path, frontmatter):
    # Empty (min_length=1) and absent (required) both surface via the "invalid frontmatter" path.
    _write_reviewer_md(tmp_path, "python", frontmatter)
    with pytest.raises(ResolveError, match="invalid frontmatter"):
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
    # Guards the repo's own .aeview/reviewers/* configs: each resolves (valid frontmatter
    # harnesses, dir == frontmatter name) and names a supported harness. Models aren't validated
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
        # If the reviewer declares its own harnesses, the resolved set must be exactly those —
        # not the test fallback, which would mask a dropped/typo'd frontmatter harnesses block.
        front, _ = parse_reviewer(reviewer.source / "REVIEWER.md")
        if front.harnesses is not None:
            assert [
                (r.instance.harness, r.instance.model, r.instance.thinking)
                for r in reviewer.harnesses
            ] == [(h.harness, h.model, h.thinking) for h in front.harnesses]


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
