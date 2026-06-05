"""Fan one prompt across the roster, one asyncio task per review.

Each worker is the sole writer of its reviews/<id>.json: it persists `running` before
the harness call and a terminal `done`/`failed` after, so a killed run leaves a truthful
status on disk (resume re-runs anything non-terminal). I1 has no retry yet (I3/I4).
"""

from __future__ import annotations

import asyncio

from .harness import AdapterError, get_adapter
from .runstore import RunStore, now_iso
from .schema import ReviewResult, RosterEntry


async def _run_review(store: RunStore, entry: RosterEntry, prompt: str, cwd) -> ReviewResult:
    result = ReviewResult(
        id=entry.id,
        reviewer=entry.reviewer,
        harness=entry.harness,
        model=entry.model,
        status="running",
        started_at=now_iso(),
    )
    store.write_review(result)

    try:
        adapter = get_adapter(entry.harness)
        out = await adapter.run(prompt, entry.model, cwd, store.log_path(entry.id))
    except AdapterError as exc:
        result.status = "failed"
        result.finished_at = now_iso()
        result.error = str(exc)
        store.write_review(result)
        return result

    result.status = "done"
    result.finished_at = now_iso()
    result.verdict = out.review.verdict
    result.summary = out.review.summary
    result.findings = out.review.findings
    result.next_steps = out.review.next_steps
    result.usage = out.usage
    store.write_review(result)
    return result


async def fan_out(
    store: RunStore,
    roster: list[RosterEntry],
    prompt_by_reviewer: dict[str, str],
    cwd,
) -> list[ReviewResult]:
    tasks = [
        asyncio.create_task(_run_review(store, entry, prompt_by_reviewer[entry.reviewer], cwd))
        for entry in roster
    ]
    return list(await asyncio.gather(*tasks))
