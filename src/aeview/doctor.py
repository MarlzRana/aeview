"""Preflight checks for `aeview doctor`: harness binaries, no-cost auth, and config validity.

Only harnesses actually referenced by a discoverable reviewer (plus the dedup harness) are
checked — codex's absence isn't a problem if nothing uses it. Auth uses each CLI's no-cost
status command (`claude auth status`, `codex login status`, `gh auth status`); no model calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from typing import Literal

from .config import Settings
from .harness import AdapterError, get_adapter
from .harness.base import AUTH_PROBE_TIMEOUT
from .process import run_sync
from .resolve import ResolveError, discover_reviewers, resolve_reviewer

CheckStatus = Literal["ok", "warn", "fail"]


@dataclass(slots=True)
class Check:
    name: str
    status: CheckStatus
    detail: str


@dataclass(slots=True)
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)


def run_doctor(cwd: Path, settings: Settings) -> DoctorReport:
    checks: list[Check] = []
    harness_types: set[str] = set()

    names = discover_reviewers(cwd)
    if not names:
        checks.append(Check("reviewers", "warn", "no reviewers found via the walk-up"))
    for name in names:
        try:
            reviewer = resolve_reviewer(name, cwd, settings)
        except ResolveError as exc:
            checks.append(Check(f"reviewer:{name}", "fail", str(exc)))
            continue
        harness_types.update(ref.instance.harness for ref in reviewer.harnesses)
        checks.append(Check(f"reviewer:{name}", "ok", f"{len(reviewer.harnesses)} harness(es)"))

    if settings.deduplication_harness:
        harness_types.add(settings.deduplication_harness.harness)
    elif names:
        checks.append(Check("dedup", "warn", "no deduplicationHarness configured"))

    for harness in sorted(harness_types):
        checks.append(_check_harness(harness, settings.harness_binaries.get(harness)))

    checks.append(_check_gh())
    return DoctorReport(checks)


def _check_harness(harness: str, binary_override: str | None) -> Check:
    name = f"harness:{harness}"
    try:
        adapter = get_adapter(harness, binary_override)
    except AdapterError as exc:
        return Check(name, "fail", str(exc))
    # Each adapter owns its check: claude verifies its SDK-resolved (possibly bundled) binary;
    # codex/copilot use the shared PATH+auth probe. The harnessBinaries override is already
    # baked into the constructed adapter.
    pf = adapter.preflight()
    return Check(name, pf.status, pf.detail)


def _check_gh() -> Check:
    if which("gh") is None:
        return Check("gh", "warn", "gh not found (needed only for --scope pr)")
    if run_sync(["gh", "auth", "status"], timeout=AUTH_PROBE_TIMEOUT).returncode == 0:
        return Check("gh", "ok", "present and authenticated")
    return Check("gh", "warn", "present but not authenticated")
