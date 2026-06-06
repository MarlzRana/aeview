"""Merge per-review results into one report.

The fan-out produces one ReviewResult per review. Merge pools every finding (tagging each
with a stable run-local id), asks the dedup harness which ids describe the same issue, and
keeps one *verbatim* survivor per group with its sources[]/agreement — then computes the
report-level fields (verdict/summary/next_steps/coverage/usage) mechanically, no model call.

Dedup runs only when more than one review contributed (you need at least two to have a
duplicate). On dedup failure — harness error/timeout, or no harness configured when one is
needed — the raw union of findings is emitted with a loud `dedup` notice; review work is
never discarded. Findings are never rewritten.
"""

from __future__ import annotations

from pathlib import Path

from .config import Settings
from .dedup import DEDUP_TIMEOUT_S, run_dedup
from .runstore import RunStore
from .schema import (
    Coverage,
    Dedup,
    DuplicateGroup,
    Finding,
    MergedFinding,
    NextStepBlock,
    PooledFinding,
    Report,
    ReviewResult,
    Source,
    Usage,
    UsageBreakdown,
    Verdict,
)

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_NO_SEVERITY = 9  # sorts after every real severity

_NO_HARNESS_WARNING = (
    "Duplicates were NOT removed — this run has multiple reviews but no deduplicationHarness "
    "is configured. Set settings.deduplicationHarness and re-run."
)

# id -> (review id, the finding as the reviewer emitted it)
_ById = dict[str, tuple[str, Finding]]


async def merge_reviews(
    results: list[ReviewResult], settings: Settings, store: RunStore, cwd: Path
) -> Report:
    done = [r for r in results if r.status == "done"]
    failed = [r for r in results if r.status != "done"]

    pool, by_id = _build_pool(done)
    dedup, findings, dedup_usage = await _dedup_and_apply(pool, by_id, done, settings, store, cwd)
    findings.sort(key=_finding_sort_key)

    reviews_usage = _sum_usage(done)
    return Report(
        verdict=_verdict(findings, done),
        summary=_summary(findings, done, failed),
        findings=findings,
        next_steps=_next_steps(done),
        coverage=Coverage(contributed=len(done), failed=len(failed)),
        dedup=dedup,
        usage=_breakdown(reviews_usage, dedup_usage),
    )


def _build_pool(done: list[ReviewResult]) -> tuple[list[PooledFinding], _ById]:
    pool: list[PooledFinding] = []
    by_id: _ById = {}
    n = 0
    for review in done:
        for finding in review.findings:
            n += 1
            fid = f"f{n}"
            pool.append(PooledFinding(id=fid, **finding.model_dump()))
            by_id[fid] = (review.id, finding)
    return pool, by_id


async def _dedup_and_apply(
    pool: list[PooledFinding],
    by_id: _ById,
    done: list[ReviewResult],
    settings: Settings,
    store: RunStore,
    cwd: Path,
) -> tuple[Dedup, list[MergedFinding], Usage]:
    # Dedup is meaningful only with >1 contributing review (the corroboration signal) *and*
    # >1 finding (something that could be a duplicate); otherwise pass through unchanged and
    # skip the billed harness call.
    if len(done) <= 1 or len(pool) <= 1:
        return Dedup(status="skipped"), _raw_union(pool, by_id), Usage()

    instance = settings.deduplication_harness
    if instance is None:
        return (
            Dedup(status="failed", reason="no deduplicationHarness configured",
                  warning=_NO_HARNESS_WARNING),
            _raw_union(pool, by_id),
            Usage(),
        )

    outcome = await run_dedup(pool, instance, store, cwd, DEDUP_TIMEOUT_S)
    if outcome.status != "ok":
        return (
            Dedup(status="failed", harness=outcome.harness_id, reason=outcome.reason,
                  warning=outcome.warning),
            _raw_union(pool, by_id),
            outcome.usage,
        )
    return (
        Dedup(status="ok", harness=outcome.harness_id),
        _apply_groups(outcome.groups, pool, by_id),
        outcome.usage,
    )


def _apply_groups(
    groups: list[DuplicateGroup], pool: list[PooledFinding], by_id: _ById
) -> list[MergedFinding]:
    consumed: set[str] = set()
    merged: list[MergedFinding] = []
    for group in groups:
        members = _valid_members(group, by_id, consumed)
        if not members:
            continue  # all ids unknown or already grouped (harness output is hostile input)
        consumed.update(members)
        survivor_id = _choose_survivor(group.survivor, members, by_id)
        _, survivor_finding = by_id[survivor_id]
        merged.append(
            MergedFinding(
                id=survivor_id,
                **survivor_finding.model_dump(),
                sources=[_source(by_id[m]) for m in members],
                agreement=len(members),
            )
        )
    # Anything the harness didn't mention is its own group (in pool order).
    for pf in pool:
        if pf.id not in consumed:
            merged.append(_singleton(pf.id, by_id[pf.id]))
    return merged


def _valid_members(group: DuplicateGroup, by_id: _ById, consumed: set[str]) -> list[str]:
    members: list[str] = []
    for mid in (group.survivor, *group.duplicates):
        if mid in by_id and mid not in consumed and mid not in members:
            members.append(mid)
    return members


def _choose_survivor(nominee: str, members: list[str], by_id: _ById) -> str:
    # The harness nominates; aeview validates it's a real member, else falls back to the
    # strongest finding (severity -> confidence -> id) — the choice only affects which verbatim
    # prose/severity is displayed, never sources[]/agreement.
    if nominee in members:
        return nominee

    def strength(mid: str) -> tuple[int, float, str]:
        _, finding = by_id[mid]
        return (_SEVERITY_ORDER.get(finding.severity, _NO_SEVERITY), -finding.confidence, mid)

    return min(members, key=strength)


def _raw_union(pool: list[PooledFinding], by_id: _ById) -> list[MergedFinding]:
    return [_singleton(pf.id, by_id[pf.id]) for pf in pool]


def _singleton(fid: str, origin: tuple[str, Finding]) -> MergedFinding:
    _, finding = origin
    return MergedFinding(
        id=fid, **finding.model_dump(), sources=[_source(origin)], agreement=1
    )


def _source(origin: tuple[str, Finding]) -> Source:
    review_id, finding = origin
    return Source(review=review_id, severity=finding.severity, confidence=finding.confidence)


def _finding_sort_key(f: MergedFinding):
    return (
        _SEVERITY_ORDER.get(f.severity, _NO_SEVERITY),
        -f.agreement,
        -f.confidence,
        f.location.file,
        f.location.line_start,
    )


def _next_steps(done: list[ReviewResult]) -> list[NextStepBlock]:
    # Per-review blocks, kept verbatim; ordered by each review's strongest finding, id tiebreak.
    with_steps = [r for r in done if r.next_steps]
    with_steps.sort(key=lambda r: (_strongest_severity(r), r.id))
    return [NextStepBlock(source=r.id, steps=r.next_steps) for r in with_steps]


def _strongest_severity(review: ReviewResult) -> int:
    if not review.findings:
        return _NO_SEVERITY
    return min(_SEVERITY_ORDER.get(f.severity, _NO_SEVERITY) for f in review.findings)


def _verdict(findings: list[MergedFinding], done: list[ReviewResult]) -> Verdict:
    # Every review failed -> there is no verdict to trust; never a green approve.
    if not done:
        return "needs-attention"
    if findings or any(r.verdict == "needs-attention" for r in done):
        return "needs-attention"
    return "approve"


def _summary(findings: list[MergedFinding], done: list[ReviewResult], failed: list) -> str:
    n = len(findings)
    noun = "finding" if n == 1 else "findings"
    review_word = "review" if len(done) == 1 else "reviews"
    base = f"{n} {noun} across {len(done)} {review_word}"
    # Corroboration is cross-review agreement: count findings raised by >1 *distinct* review,
    # not raw group size — one reviewer repeating itself isn't corroboration.
    corroborated = sum(1 for f in findings if len({s.review for s in f.sources}) > 1)
    if corroborated:
        base += f"; {corroborated} corroborated"
    if failed:
        base += f"; {len(failed)} failed"
    return base + "."


def _sum_usage(reviews: list[ReviewResult]) -> Usage:
    return Usage(
        input_tokens=sum(r.usage.input_tokens for r in reviews),
        output_tokens=sum(r.usage.output_tokens for r in reviews),
        cost_usd=round(sum(r.usage.cost_usd for r in reviews), 6),
    )


def _breakdown(reviews: Usage, dedup: Usage) -> UsageBreakdown:
    total = Usage(
        input_tokens=reviews.input_tokens + dedup.input_tokens,
        output_tokens=reviews.output_tokens + dedup.output_tokens,
        cost_usd=round(reviews.cost_usd + dedup.cost_usd, 6),
    )
    return UsageBreakdown(reviews=reviews, dedup=dedup, total=total)
