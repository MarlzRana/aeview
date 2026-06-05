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

# Files seeded into ~/.aeview from src/aeview/_data on first run (write-if-absent).
SEED_FILES = ("settings.json", "REVIEWER.md", "DEDUPLICATION.md")


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


class Retention(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    keep_last: int = 20
    ttl_days: int = 14


class Settings(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    default_harnesses: list[HarnessInstance] = Field(default_factory=list)
    deduplication_harness: HarnessInstance | None = None
    retention: Retention = Field(default_factory=Retention)


def _package_data(name: str) -> str:
    return (files("aeview._data") / name).read_text(encoding="utf-8")


def ensure_seeded() -> Path:
    """Write any missing default files into ~/.aeview. Idempotent, never clobbers."""
    home = aeview_home()
    home.mkdir(parents=True, exist_ok=True)
    runs_dir().mkdir(parents=True, exist_ok=True)
    for name in SEED_FILES:
        target = home / name
        if not target.exists():
            target.write_text(_package_data(name), encoding="utf-8")
    return home


def load_settings() -> Settings:
    ensure_seeded()
    raw = json.loads((aeview_home() / "settings.json").read_text(encoding="utf-8"))
    return Settings.model_validate(raw)


def default_reviewer_path() -> Path:
    """The global fallback reviewer prompt (~/.aeview/REVIEWER.md)."""
    ensure_seeded()
    return aeview_home() / "REVIEWER.md"
