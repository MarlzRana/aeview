from __future__ import annotations

from aeview.config import HarnessInstance, ensure_seeded, load_dedup_prompt, load_settings


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


def test_load_dedup_prompt_strips_frontmatter(aeview_home):
    # The seeded DEDUPLICATION.md has YAML frontmatter; only the body should reach the harness.
    body = load_dedup_prompt()
    assert not body.startswith("---")
    assert "name: deduplication" not in body
    assert "same underlying" in body  # body content present


def test_load_dedup_prompt_returns_plain_text_unchanged(aeview_home):
    ensure_seeded()
    (aeview_home / "DEDUPLICATION.md").write_text("no frontmatter here\njust body\n")
    assert load_dedup_prompt() == "no frontmatter here\njust body\n"


def test_load_dedup_prompt_unterminated_frontmatter_returned_unchanged(aeview_home):
    # Opens '---' but never closes -> not valid frontmatter -> return the text as-is (lenient),
    # rather than silently dropping the leading lines.
    ensure_seeded()
    text = "---\nname: x\nstill no close\n"
    (aeview_home / "DEDUPLICATION.md").write_text(text)
    assert load_dedup_prompt() == text


def test_descriptor_id_includes_thinking_only_when_set():
    def descriptor(**kw) -> str:
        return HarnessInstance(harness="claude-code", model="opus", **kw).descriptor_id

    assert descriptor() == "claude-code-opus"
    assert descriptor(thinking="default") == "claude-code-opus"  # "default" means unset
    assert descriptor(thinking="high") == "claude-code-opus-high"
