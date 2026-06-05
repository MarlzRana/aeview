from __future__ import annotations

from aeview.merge import merge_reviews
from aeview.schema import Finding, Location, ReviewResult, Severity, Usage, Verdict


def _finding(title: str, severity: Severity) -> Finding:
    return Finding(
        title=title,
        body="b",
        severity=severity,
        category="bug",
        confidence=0.5,
        location=Location(file="a.py", line_start=1, line_end=1),
        recommendation="fix",
    )


def _done(rid: str, findings: list[Finding], verdict: Verdict) -> ReviewResult:
    return ReviewResult(
        id=rid,
        reviewer="default",
        harness="claude-code",
        model="sonnet",
        status="done",
        verdict=verdict,
        summary="s",
        findings=findings,
        next_steps=["step"],
        usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.01),
    )


def test_single_review_passthrough_has_provenance():
    review = _done("default__claude-code-sonnet", [_finding("x", "high")], "needs-attention")
    report = merge_reviews([review])
    assert report.verdict == "needs-attention"
    assert report.dedup.status == "skipped"
    assert report.coverage.contributed == 1
    assert report.findings[0].agreement == 1
    assert report.findings[0].sources[0].review == "default__claude-code-sonnet"
    assert report.usage.cost_usd == 0.01


def test_approve_when_no_findings():
    report = merge_reviews([_done("r", [], "approve")])
    assert report.verdict == "approve"
    assert report.findings == []


def test_findings_sorted_by_severity():
    report = merge_reviews(
        [_done("r", [_finding("low", "low"), _finding("crit", "critical")], "needs-attention")]
    )
    assert [f.severity for f in report.findings] == ["critical", "low"]


def test_failed_review_counted_in_coverage():
    failed = ReviewResult(
        id="r2", reviewer="default", harness="claude-code", model="sonnet", status="failed",
        error="boom",
    )
    report = merge_reviews([_done("r1", [_finding("x", "high")], "needs-attention"), failed])
    assert report.coverage.contributed == 1
    assert report.coverage.failed == 1
    assert "1 failed" in report.summary
