"""Global config at ~/.aeview and idempotent self-seeding.

There are no install-time hooks (wheels, brew, uv/pipx all run nothing post-install).
So `ensure_seeded()` runs at the start of every invocation: it writes any *missing*
default into ~/.aeview from bundled package data, never clobbering user edits.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# Package-data file -> path under ~/.aeview, seeded on first run (write-if-absent).
# REVIEWER.md lands inside reviewers/default/ so the default reviewer resolves through the
# same uniform walk-up as any other reviewer; settings/dedup stay at the ~/.aeview root.
SEED_FILES = {
    "settings.json": "settings.json",
    "DEDUPLICATION.md": "DEDUPLICATION.md",
    "REVIEWER.md": "reviewers/default/REVIEWER.md",
    "harness.json": "reviewers/default/harness.json",
}


def aeview_home() -> Path:
    return Path.home() / ".aeview"


def runs_dir() -> Path:
    return aeview_home() / "runs"


class HarnessInstance(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    harness: str
    model: str
    thinking: str | None = None

    @property
    def instance_id(self) -> str:
        return f"{self.harness}-{self.model}"

    @property
    def descriptor_id(self) -> str:
        """Like instance_id but always includes thinking when set — used for the single dedup
        instance's on-disk dir and run.json record (no collision-escalation needed there)."""
        if self.thinking and self.thinking != "default":
            return f"{self.harness}-{self.model}-{self.thinking}"
        return self.instance_id


class Retention(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    # Validated once at the settings.json boundary so prune never clamps. keepLast and ttlDays
    # are independent prune triggers, not guaranteed minimums: a run is pruned if it's outside
    # the newest keepLast OR older than ttlDays (see prune_runs). ttlDays >= 1 (0 would expire
    # every terminal run); keepLast 0 disables the count trigger.
    keep_last: int = Field(default=20, ge=0)
    ttl_days: int = Field(default=14, ge=1)


class Settings(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    # The harnesses a reviewer runs on when it ships no co-located harness.json.
    fallback_reviewer_harnesses: list[HarnessInstance] = Field(default_factory=list)
    deduplication_harness: HarnessInstance | None = None
    retention: Retention = Field(default_factory=Retention)
    # Per-review wall-clock bound. On expiry the harness child is killed and that review is
    # marked failed (fail-fast, no retry); `resume` can re-run it. Generous by default — model
    # reviews of a large diff are slow.
    review_timeout_seconds: int = Field(default=1200, ge=1)


def _package_data(name: str) -> str:
    return (files("aeview._data") / name).read_text(encoding="utf-8")


def ensure_seeded() -> Path:
    """Write any missing default files into ~/.aeview. Idempotent, never clobbers."""
    home = aeview_home()
    home.mkdir(parents=True, exist_ok=True)
    runs_dir().mkdir(parents=True, exist_ok=True)
    for src_name, rel_target in SEED_FILES.items():
        target = home / rel_target
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_package_data(src_name), encoding="utf-8")
    return home


def load_settings() -> Settings:
    ensure_seeded()
    raw = json.loads((aeview_home() / "settings.json").read_text(encoding="utf-8"))
    return Settings.model_validate(raw)


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split leading `---\\n...\\n---` YAML frontmatter from the body.

    Returns (front, body). `front` is None when there's no opening `---` or no closing `---`
    — the body is then the full text unchanged. Callers decide whether a missing front is an
    error (reviewers) or fine (the dedup prompt). Keeps the `---` convention in one place."""
    if not text.startswith("---"):
        return None, text
    _, _, rest = text.partition("---\n")
    front, sep, body = rest.partition("\n---")
    if not sep:
        return None, text
    return front, body.lstrip("\n")


def load_dedup_prompt() -> str:
    """The dedup harness instructions: ~/.aeview/DEDUPLICATION.md, frontmatter stripped."""
    ensure_seeded()
    text = (aeview_home() / "DEDUPLICATION.md").read_text(encoding="utf-8")
    return split_frontmatter(text)[1]
