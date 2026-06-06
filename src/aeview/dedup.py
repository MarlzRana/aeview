"""Run the deduplication harness over the pooled findings.

The dedup harness is findings-only: it sees a flat list of findings (each tagged with a
stable id) plus the DEDUPLICATION.md prompt, and returns id-groups ({survivor, duplicates}).
It never sees verdict/summary/next_steps and never produces them — it only identifies which
findings are the same issue. This module owns the harness call + artifact writing; merge.py
applies the returned groups. On any failure (harness error, timeout, invalid output) the
outcome is `failed`, and merge emits the raw union of findings plus a loud notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from .config import HarnessInstance, load_dedup_prompt
from .harness import AdapterError, get_adapter
from .runstore import RunStore, now_iso, pool_to_json
from .schema import (
    DedupResult,
    DuplicateGroup,
    DuplicateGroups,
    PooledFinding,
    Usage,
    duplicate_groups_json_schema,
)

DEDUP_TIMEOUT_S = 600.0

_FAIL_WARNING = (
    "Duplicates were NOT removed — findings may repeat across reviews. "
    "Fix the dedup harness and re-run."
)


@dataclass(slots=True)
class DedupOutcome:
    """The result merge needs: groups to apply (ok) or a failure to surface (failed).

    An outcome is never "skipped" — that gate lives in merge before run_dedup is called.
    """

    status: Literal["ok", "failed"]
    groups: list[DuplicateGroup]
    usage: Usage
    harness_id: str
    reason: str | None = None
    warning: str | None = None


async def run_dedup(
    pool: list[PooledFinding],
    instance: HarnessInstance,
    store: RunStore,
    cwd: Path,
    timeout: float = DEDUP_TIMEOUT_S,
) -> DedupOutcome:
    instance_id = instance.descriptor_id
    prompt = _compose(pool)
    store.write_dedup_prompt(instance_id, prompt)
    store.write_dedup_input(instance_id, pool)

    started = now_iso()
    try:
        adapter = get_adapter(instance.harness)
        out = await adapter.run_structured(
            prompt,
            duplicate_groups_json_schema(),
            instance.model,
            cwd,
            store.dedup_log_path(instance_id),
            instance.thinking,
            timeout,
        )
        groups = DuplicateGroups.model_validate(out.payload).duplicate_groups
    except (AdapterError, ValidationError) as exc:
        outcome = DedupOutcome(
            "failed", [], Usage(), instance_id, reason=str(exc), warning=_FAIL_WARNING
        )
        _persist(store, outcome, started)
        return outcome

    outcome = DedupOutcome("ok", groups, out.usage, instance_id)
    _persist(store, outcome, started)
    return outcome


def _compose(pool: list[PooledFinding]) -> str:
    return (
        f"{load_dedup_prompt().rstrip()}\n\n"
        f"## Findings to deduplicate\n\n"
        f"```json\n{pool_to_json(pool)}\n```\n"
    )


def _persist(store: RunStore, outcome: DedupOutcome, started: str) -> None:
    store.write_dedup_result(
        outcome.harness_id,
        DedupResult(
            harness=outcome.harness_id,
            status=outcome.status,
            started_at=started,
            finished_at=now_iso(),
            groups=outcome.groups,
            usage=outcome.usage,
            reason=outcome.reason,
            warning=outcome.warning,
        ),
    )
