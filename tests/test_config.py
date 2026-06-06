from __future__ import annotations

from aeview.config import ensure_seeded, load_settings


def test_ensure_seeded_writes_defaults(aeview_home):
    ensure_seeded()
    assert (aeview_home / "settings.json").exists()
    assert (aeview_home / "DEDUPLICATION.md").exists()
    assert (aeview_home / "reviewers" / "default" / "REVIEWER.md").exists()
    assert (aeview_home / "reviewers" / "default" / "harness.json").exists()


def test_ensure_seeded_never_clobbers(aeview_home):
    ensure_seeded()
    reviewer = aeview_home / "reviewers" / "default" / "REVIEWER.md"
    custom = "---\nname: default\ndescription: mine\n---\nmy prompt"
    reviewer.write_text(custom)
    ensure_seeded()
    assert reviewer.read_text() == custom


def test_load_settings_parses_camel_case(aeview_home):
    settings = load_settings()
    assert settings.fallback_reviewer_harnesses[0].harness == "claude-code"
    assert settings.fallback_reviewer_harnesses[0].instance_id == "claude-code-claude-opus-4-8"
