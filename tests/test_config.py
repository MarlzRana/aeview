from __future__ import annotations

from aeview.config import ensure_seeded, load_settings


def test_ensure_seeded_writes_defaults(aeview_home):
    ensure_seeded()
    assert (aeview_home / "settings.json").exists()
    assert (aeview_home / "REVIEWER.md").exists()
    assert (aeview_home / "DEDUPLICATION.md").exists()


def test_ensure_seeded_never_clobbers(aeview_home):
    ensure_seeded()
    custom = "---\nname: default\ndescription: mine\n---\nmy prompt"
    (aeview_home / "REVIEWER.md").write_text(custom)
    ensure_seeded()
    assert (aeview_home / "REVIEWER.md").read_text() == custom


def test_load_settings_parses_camel_case(aeview_home):
    settings = load_settings()
    assert settings.default_harnesses[0].harness == "claude-code"
    assert settings.default_harnesses[0].instance_id == "claude-code-sonnet"
