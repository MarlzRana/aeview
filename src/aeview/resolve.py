"""Resolve reviewer names to prompts and build the run roster.

Increment 1 resolves only the `default` reviewer to the global ~/.aeview/REVIEWER.md.
The full walk-up (first-match-wins from CWD, dir == name validation, per-repo override,
harness.json) arrives in I3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import HarnessInstance, Settings, default_reviewer_path
from .schema import RosterEntry


@dataclass(slots=True)
class Reviewer:
    name: str
    description: str
    body: str
    harnesses: list[HarnessInstance]


class ResolveError(Exception):
    """Raised when a reviewer cannot be resolved."""


def parse_reviewer(path: Path) -> tuple[str, str, str]:
    """Split a REVIEWER.md into (name, description, body) using YAML frontmatter."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ResolveError(f"{path} is missing YAML frontmatter")
    _, _, rest = text.partition("---\n")
    front, sep, body = rest.partition("\n---")
    if not sep:
        raise ResolveError(f"{path} has malformed frontmatter")
    meta = yaml.safe_load(front) or {}
    name = meta.get("name")
    description = meta.get("description", "")
    if not name:
        raise ResolveError(f"{path} frontmatter is missing 'name'")
    return str(name), str(description), body.lstrip("\n")


def resolve_reviewer(name: str, settings: Settings) -> Reviewer:
    if name != "default":
        raise ResolveError(
            f"reviewer '{name}' not found; only 'default' is supported in this build"
        )
    rname, description, body = parse_reviewer(default_reviewer_path())
    return Reviewer(
        name=rname,
        description=description,
        body=body,
        harnesses=list(settings.default_harnesses),
    )


def build_roster(reviewers: list[Reviewer]) -> list[RosterEntry]:
    roster: list[RosterEntry] = []
    for reviewer in reviewers:
        for harness in reviewer.harnesses:
            roster.append(
                RosterEntry(
                    id=f"{reviewer.name}__{harness.instance_id}",
                    reviewer=reviewer.name,
                    harness=harness.harness,
                    model=harness.model,
                )
            )
    return roster
