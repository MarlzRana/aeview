"""Resolve reviewer names to prompts + harness instances via a uniform directory walk-up.

For each name, climb from CWD through its ancestors (terminating at home) looking for
`<rung>/.aeview/reviewers/<name>/REVIEWER.md` — first match wins. Home's `.aeview` is the
global config dir `~/.aeview/`, so the same climb reaches `~/.aeview/reviewers/<name>/`
with no special case. `.agents/` is reserved for shared, standardized conventions.

A reviewer's harnesses come from a `harnesses:` block in its REVIEWER.md frontmatter;
absent → the global `fallbackReviewerHarnesses` in settings.json. The dir name must equal
the frontmatter `name`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import HarnessInstance, Settings, split_frontmatter
from .schema import RosterEntry

REVIEWER_FILE = "REVIEWER.md"
_AEVIEW_DIR = ".aeview"
_REVIEWERS = "reviewers"
# Names that can't be reviewers because they're CLI keywords. `all` is the bulk-sweep
# keyword for --reviewers, so a reviewer named `all` would be unreachable by name.
RESERVED_REVIEWER_NAMES = {"all"}


class ResolveError(Exception):
    """Raised when a reviewer cannot be resolved or its config is invalid."""


@dataclass(slots=True)
class HarnessRef:
    """A harness instance plus its collision-resolved id (unique within a reviewer)."""

    instance: HarnessInstance
    id: str


class ReviewerFrontMatter(BaseModel):
    """The validated YAML frontmatter of a REVIEWER.md. `harnesses` (N1) lives here, not in a
    separate file; `extra="forbid"` turns a typo'd key (e.g. `harneses:`) into a clear error."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    description: str = ""
    harnesses: list[HarnessInstance] | None = None
    # Accepted + validated here so the frontmatter contract is stable across the batch; the
    # activation logic that consumes it is wired in N3 (I9 — auto-activate-paths).
    auto_activate_paths: list[str] | None = Field(default=None, alias="auto-activate-paths")


@dataclass(slots=True)
class Reviewer:
    name: str
    description: str
    body: str
    source: Path  # the reviewer directory the prompt was loaded from
    harnesses: list[HarnessRef]


def parse_reviewer(path: Path) -> tuple[ReviewerFrontMatter, str]:
    """Parse a REVIEWER.md into its validated frontmatter + body."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # Discovery passed is_file(), but the file can be unreadable at read time (mode 000, or
        # removed in the gap). Normalize to ResolveError so the sweep + `reviewers` stay resilient.
        raise ResolveError(f"{path} could not be read: {exc}") from exc
    front, body = split_frontmatter(text)
    if front is None:
        raise ResolveError(f"{path} is missing or has malformed YAML frontmatter")
    try:
        meta = yaml.safe_load(front)
    except yaml.YAMLError as exc:
        # Normalize to ResolveError so --reviewers all leniency catches it uniformly.
        raise ResolveError(f"{path} has invalid YAML frontmatter: {exc}") from exc
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ResolveError(f"{path} frontmatter must be a mapping")
    try:
        front_matter = ReviewerFrontMatter.model_validate(meta)
    except ValidationError as exc:
        raise ResolveError(f"{path} has invalid frontmatter: {exc}") from exc
    if not front_matter.name:
        raise ResolveError(f"{path} frontmatter is missing 'name'")
    return front_matter, body  # body already had its leading newlines stripped


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


@dataclass(slots=True)
class DiscoveredReviewer:
    """A reviewer name seen via the walk-up: the winning (nearest) dir plus any farther dirs
    that define the same name and are therefore shadowed (surfaced by `aeview reviewers`)."""

    name: str
    source: Path  # nearest rung's reviewer dir — the one that wins
    shadowed: list[Path]  # farther rungs defining the same name (not used)


def discover_reviewer_sources(cwd: Path) -> list[DiscoveredReviewer]:
    """Every reviewer name visible here, nearest-first, with its winning dir + shadowed dirs."""
    found: dict[str, DiscoveredReviewer] = {}  # insertion order = nearest-first discovery order
    for rung in _candidate_rungs(cwd):
        parent = rung / _AEVIEW_DIR / _REVIEWERS
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if not (child / REVIEWER_FILE).is_file():
                continue
            existing = found.get(child.name)
            if existing is None:
                found[child.name] = DiscoveredReviewer(child.name, child, [])
            else:
                existing.shadowed.append(child)  # a farther rung shadowed by the nearer one
    return list(found.values())


def discover_reviewers(cwd: Path) -> list[str]:
    """All reviewer names visible here via the walk-up, nearest-first, first-match-wins."""
    return [d.name for d in discover_reviewer_sources(cwd)]


def _load_reviewer(reviewer_dir: Path, dir_name: str, settings: Settings) -> Reviewer:
    front, body = parse_reviewer(reviewer_dir / REVIEWER_FILE)
    if front.name != dir_name:
        raise ResolveError(
            f"reviewer directory '{dir_name}' does not match its REVIEWER.md name "
            f"'{front.name}' ({reviewer_dir})"
        )
    if front.name in RESERVED_REVIEWER_NAMES:
        raise ResolveError(
            f"'{front.name}' is a reserved reviewer name (it's a --reviewers keyword)"
        )
    return Reviewer(
        name=front.name,
        description=front.description,
        body=body,
        source=reviewer_dir,
        harnesses=_resolve_harnesses(front, reviewer_dir, settings),
    )


def _resolve_harnesses(
    front: ReviewerFrontMatter, reviewer_dir: Path, settings: Settings
) -> list[HarnessRef]:
    # A `harnesses:` block (even an empty list) is the reviewer's own choice; only its absence
    # falls back to the global default. Per-entry validation already ran in parse_reviewer.
    if front.harnesses is not None:
        if not front.harnesses:
            raise ResolveError(f"{reviewer_dir / REVIEWER_FILE} lists no harnesses")
        return _assign_ids(front.harnesses)
    if not settings.fallback_reviewer_harnesses:
        raise ResolveError(
            f"{reviewer_dir} has no harnesses: in its REVIEWER.md and "
            f"settings.fallbackReviewerHarnesses is empty"
        )
    return _assign_ids(settings.fallback_reviewer_harnesses)


def _assign_ids(instances: list[HarnessInstance]) -> list[HarnessRef]:
    """Derive a unique id per instance: harness-model, escalating to +thinking then -N.

    Every id is uniquified against the ids already assigned, so a non-escalated base id
    (e.g. a model literally named `opus-high`) can never collide with another instance's
    escalated id (`opus` + thinking `high`) — duplicate ids would clobber review files.
    """
    base = [f"{i.harness}-{i.model}" for i in instances]
    base_counts = Counter(base)
    used: set[str] = set()
    refs: list[HarnessRef] = []
    for inst, b in zip(instances, base, strict=True):
        candidate = b if base_counts[b] == 1 else f"{b}-{inst.thinking or 'default'}"
        rid = _uniquify(candidate, used)
        used.add(rid)
        refs.append(HarnessRef(instance=inst, id=rid))
    return refs


def _uniquify(candidate: str, used: set[str]) -> str:
    if candidate not in used:
        return candidate
    n = 2
    while f"{candidate}-{n}" in used:
        n += 1
    return f"{candidate}-{n}"


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
                    thinking=ref.instance.thinking,
                )
            )
    return roster
