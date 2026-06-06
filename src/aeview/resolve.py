"""Resolve reviewer names to prompts + harness instances via a uniform directory walk-up.

For each name, climb from CWD through its ancestors (terminating at home) looking for
`<rung>/.aeview/reviewers/<name>/REVIEWER.md` — first match wins. Home's `.aeview` is the
global config dir `~/.aeview/`, so the same climb reaches `~/.aeview/reviewers/<name>/`
with no special case. `.agents/` is reserved for shared, standardized conventions.

A reviewer's harnesses come from a co-located `harness.json`; absent → the global
`fallbackReviewerHarnesses` in settings.json. The dir name must equal the frontmatter
`name`.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import HarnessInstance, Settings
from .schema import RosterEntry

REVIEWER_FILE = "REVIEWER.md"
HARNESS_FILE = "harness.json"
_AEVIEW_DIR = ".aeview"
_REVIEWERS = "reviewers"


class ResolveError(Exception):
    """Raised when a reviewer cannot be resolved or its config is invalid."""


@dataclass(slots=True)
class HarnessRef:
    """A harness instance plus its collision-resolved id (unique within a reviewer)."""

    instance: HarnessInstance
    id: str


@dataclass(slots=True)
class Reviewer:
    name: str
    description: str
    body: str
    source: Path  # the reviewer directory the prompt was loaded from
    harnesses: list[HarnessRef]


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


def _candidate_rungs(cwd: Path) -> list[Path]:
    """Directories to check, nearest-first: CWD up to home (or root), then home."""
    home = Path.home().resolve()
    rungs: list[Path] = []
    d = cwd.resolve()
    while True:
        rungs.append(d)
        if d == home or d == d.parent:
            break
        d = d.parent
    if home not in rungs:
        rungs.append(home)
    return rungs


def _reviewer_dir(rung: Path, name: str) -> Path:
    return rung / _AEVIEW_DIR / _REVIEWERS / name


def resolve_reviewer(name: str, cwd: Path, settings: Settings) -> Reviewer:
    for rung in _candidate_rungs(cwd):
        reviewer_file = _reviewer_dir(rung, name) / REVIEWER_FILE
        if reviewer_file.is_file():
            return _load_reviewer(reviewer_file.parent, name, settings)
    raise ResolveError(
        f"reviewer '{name}' not found "
        f"(no .aeview/reviewers/{name}/REVIEWER.md from {cwd} up to ~/.aeview)"
    )


def discover_reviewers(cwd: Path, settings: Settings) -> list[str]:
    """All reviewer names visible here via the walk-up, nearest-first, first-match-wins."""
    names: list[str] = []
    seen: set[str] = set()
    for rung in _candidate_rungs(cwd):
        parent = rung / _AEVIEW_DIR / _REVIEWERS
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.name not in seen and (child / REVIEWER_FILE).is_file():
                seen.add(child.name)
                names.append(child.name)
    return names


def _load_reviewer(reviewer_dir: Path, dir_name: str, settings: Settings) -> Reviewer:
    name, description, body = parse_reviewer(reviewer_dir / REVIEWER_FILE)
    if name != dir_name:
        raise ResolveError(
            f"reviewer directory '{dir_name}' does not match its REVIEWER.md name "
            f"'{name}' ({reviewer_dir})"
        )
    return Reviewer(
        name=name,
        description=description,
        body=body,
        source=reviewer_dir,
        harnesses=_resolve_harnesses(reviewer_dir, settings),
    )


def _resolve_harnesses(reviewer_dir: Path, settings: Settings) -> list[HarnessRef]:
    harness_file = reviewer_dir / HARNESS_FILE
    if harness_file.is_file():
        raw = json.loads(harness_file.read_text(encoding="utf-8"))
        instances = [HarnessInstance.model_validate(h) for h in raw.get("harnesses", [])]
        if not instances:
            raise ResolveError(f"{harness_file} lists no harnesses")
        return _assign_ids(instances)
    if not settings.fallback_reviewer_harnesses:
        raise ResolveError(
            f"{reviewer_dir} has no harness.json and settings.fallbackReviewerHarnesses is empty"
        )
    return _assign_ids(settings.fallback_reviewer_harnesses)


def _assign_ids(instances: list[HarnessInstance]) -> list[HarnessRef]:
    """Derive a unique id per instance: harness-model, escalating to +thinking then -N."""
    base = [f"{i.harness}-{i.model}" for i in instances]
    base_counts = Counter(base)
    used: set[str] = set()
    refs: list[HarnessRef] = []
    for inst, b in zip(instances, base, strict=True):
        if base_counts[b] == 1:
            rid = b
        else:
            rid = f"{b}-{inst.thinking or 'default'}"
            if rid in used:
                n = 2
                while f"{rid}-{n}" in used:
                    n += 1
                rid = f"{rid}-{n}"
        used.add(rid)
        refs.append(HarnessRef(instance=inst, id=rid))
    return refs


def build_roster(reviewers: list[Reviewer]) -> list[RosterEntry]:
    """The full cross-product: one roster entry per (reviewer x harness instance)."""
    roster: list[RosterEntry] = []
    for reviewer in reviewers:
        for ref in reviewer.harnesses:
            roster.append(
                RosterEntry(
                    id=f"{reviewer.name}__{ref.id}",
                    reviewer=reviewer.name,
                    harness=ref.instance.harness,
                    model=ref.instance.model,
                )
            )
    return roster
