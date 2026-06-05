"""Merge per-review results into one report.

Increment 1 skips the dedup harness (single-review rosters). Findings pass straight
through, each gaining provenance: a `sources` list of length 1 and `agreement` 1 — the
identical shape a deduplicated finding has, so the report schema is stable from day one.
The dedup harness and survivor selection arrive in I5.
"""

from __future__ import annotations

from .schema import (
    Coverage,
    Dedup,
    MergedFinding,
    NextStepBlock,
    Report,
    ReviewResult,
    Source,
    Usage,
)

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def merge_reviews(results: list[ReviewResult]) -> Report:
    done = [r for r in results if r.status == "done"]
    failed = [r for r in results if r.status != "done"]

    findings = _passthrough_findings(done)
    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), -f.agreement, -f.confidence))

    next_steps = [
        NextStepBlock(source=r.id, steps=r.next_steps) for r in done if r.next_steps
    ]

    verdict = "needs-attention" if (findings or any(
        r.verdict == "needs-attention" for r in done
    )) else "approve"

    return Report(
        verdict=verdict,
        summary=_summary(findings, done, failed),
        findings=findings,
        next_steps=next_steps,
        coverage=Coverage(contributed=len(done), failed=len(failed)),
        dedup=Dedup(status="skipped"),
        usage=_total_usage(done),
    )


def _passthrough_findings(done: list[ReviewResult]) -> list[MergedFinding]:
    merged: list[MergedFinding] = []
    for review in done:
        for finding in review.findings:
            merged.append(
                MergedFinding(
                    **finding.model_dump(),
                    sources=[
                        Source(
                            review=review.id,
                            severity=finding.severity,
                            confidence=finding.confidence,
                        )
                    ],
                    agreement=1,
                )
            )
    return merged


def _summary(findings: list[MergedFinding], done: list[ReviewResult], failed: list) -> str:
    n = len(findings)
    noun = "finding" if n == 1 else "findings"
    review_word = "review" if len(done) == 1 else "reviews"
    base = f"{n} {noun} across {len(done)} {review_word}"
    if failed:
        base += f"; {len(failed)} failed"
    return base + "."


def _total_usage(done: list[ReviewResult]) -> Usage:
    return Usage(
        input_tokens=sum(r.usage.input_tokens for r in done),
        output_tokens=sum(r.usage.output_tokens for r in done),
        cost_usd=round(sum(r.usage.cost_usd for r in done), 6),
    )
