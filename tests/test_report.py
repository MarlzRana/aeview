from __future__ import annotations

from aeview.report import EXIT_ERROR, EXIT_NEEDS_ATTENTION, exit_code, render_human
from aeview.schema import Coverage, Dedup, Report, Usage, UsageBreakdown


def _report(**over) -> Report:
    base = Report(
        verdict="needs-attention",
        summary="1 finding across 2 reviews.",
        findings=[],
        next_steps=[],
        coverage=Coverage(contributed=2, failed=0),
        dedup=Dedup(status="ok"),
        usage=UsageBreakdown(
            reviews=Usage(cost_usd=0.10), dedup=Usage(cost_usd=0.02), total=Usage(cost_usd=0.12)
        ),
    )
    return base.model_copy(update=over)


def test_render_shows_dedup_failed_notice():
    report = _report(
        dedup=Dedup(status="failed", harness="dx", reason="timed out", warning="DUPES NOT REMOVED")
    )
    out = render_human(report)
    assert "dedup FAILED: DUPES NOT REMOVED" in out


def test_render_cost_uses_usage_total():
    out = render_human(_report())
    assert "cost: $0.1200" in out  # total, not reviews-only (0.10)


def test_render_ok_dedup_has_no_failed_line():
    assert "dedup FAILED" not in render_human(_report())


def test_all_failed_renders_error_and_exits_2():
    report = _report(
        verdict="needs-attention", coverage=Coverage(contributed=0, failed=3),
        usage=UsageBreakdown(),
    )
    assert "[XX] error" in render_human(report)
    assert exit_code(report) == EXIT_ERROR


def test_needs_attention_exit_code():
    assert exit_code(_report()) == EXIT_NEEDS_ATTENTION
