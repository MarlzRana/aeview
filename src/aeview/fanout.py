"""Fan one prompt across the roster, one asyncio task per review (unbounded).

Each worker is the sole writer of its reviewers/<reviewer>/<instance>/review.json: it
persists `running` before the harness call and a terminal `done`/`failed` after, so a killed
run leaves a truthful status on disk (resume re-runs anything non-terminal).

Transient failures (rate-limit, overload, timeout) retry with exponential backoff + jitter;
non-transient ones (bad auth, missing binary, schema-invalid output) fail fast. A single
review failing never aborts the run — it is recorded in coverage.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

from .harness import AdapterError, HarnessOutput, get_adapter
from .runstore import RunStore, now_iso
from .schema import ReviewResult, RosterEntry

MAX_ATTEMPTS = 3
_BASE_DELAY_S = 1.0


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff (1s, 2s, ...) plus jitter to de-correlate retries."""
    return _BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5)  # noqa: S311 - jitter, not crypto


async def _run_review(
    store: RunStore,
    entry: RosterEntry,
    prompt: str,
    cwd: Path,
    timeout: float | None,
    harness_binaries: dict[str, str],
) -> ReviewResult:
    result = ReviewResult(
        id=entry.id,
        reviewer=entry.reviewer,
        harness=entry.harness,
        model=entry.model,
        status="running",
        started_at=now_iso(),
    )
    store.write_review(result)
    # A worker never raises: any failure becomes a failed ReviewResult, so one bad review
    # can't abort gather() and orphan its siblings (and their live subprocesses).
    try:
        return await _attempt_review(store, result, entry, prompt, cwd, timeout, harness_binaries)
    except AdapterError as exc:
        return _mark_failed(store, result, str(exc))
    except Exception as exc:  # noqa: BLE001 - last-resort guard so the run never crashes
        return _mark_failed(store, result, f"unexpected error: {exc}")


async def _attempt_review(
    store: RunStore,
    result: ReviewResult,
    entry: RosterEntry,
    prompt: str,
    cwd: Path,
    timeout: float | None,
    harness_binaries: dict[str, str],
) -> ReviewResult:
    adapter = get_adapter(entry.harness, harness_binaries.get(entry.harness))
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            out = await adapter.run(
                prompt,
                entry.model,
                cwd,
                store.log_path(entry.reviewer, entry.id),
                thinking=entry.thinking,
                timeout=timeout,
            )
        except AdapterError as exc:
            last_error = str(exc)
            if not exc.transient or attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(_backoff_delay(attempt))
            continue
        return _mark_done(store, result, out)
    return _mark_failed(store, result, last_error)


def _mark_done(store: RunStore, result: ReviewResult, out: HarnessOutput) -> ReviewResult:
    result.status = "done"
    result.finished_at = now_iso()
    result.verdict = out.review.verdict
    result.summary = out.review.summary
    result.findings = out.review.findings
    result.next_steps = out.review.next_steps
    result.usage = out.usage
    store.write_review(result)
    return result


def _mark_failed(store: RunStore, result: ReviewResult, error: str) -> ReviewResult:
    result.status = "failed"
    result.finished_at = now_iso()
    result.error = error
    store.write_review(result)
    return result


async def fan_out(
    store: RunStore,
    roster: list[RosterEntry],
    prompt_by_reviewer: dict[str, str],
    cwd: Path,
    timeout: float | None = None,
    harness_binaries: dict[str, str] | None = None,
) -> list[ReviewResult]:
    overrides = harness_binaries or {}
    tasks = [
        asyncio.create_task(
            _run_review(store, entry, prompt_by_reviewer[entry.reviewer], cwd, timeout, overrides)
        )
        for entry in roster
    ]
    return list(await asyncio.gather(*tasks))
