from __future__ import annotations

from aeview.report import (
    EXIT_ERROR,
    EXIT_NEEDS_ATTENTION,
    exit_code,
    render_human,
    run_gate_dict,
)
from aeview.schema import (
    Coverage,
    Dedup,
    Location,
    MergedFinding,
    NextStepBlock,
    Report,
    Source,
    Usage,
    UsageBreakdown,
)


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


def _finding() -> MergedFinding:
    return MergedFinding(
        id="f1",
        title="Unvalidated path",
        body="the long reasoning",
        severity="high",
        category="security",
        confidence=0.9,
        location=Location(file="a.py", line_start=42, line_end=48),
        recommendation="validate it",
        sources=[Source(review="r__codex-gpt-5.5", severity="high", confidence=0.9)],
        agreement=2,
    )


def test_run_gate_drops_result_only_fields_and_adds_run_id():
    report = _report(
        findings=[_finding()],
        next_steps=[NextStepBlock(source="r", steps=["do x"])],
        dedup=Dedup(status="ok", harness="dx", reason=None, warning=None),
    )
    gate = run_gate_dict(report, "RID")
    assert gate["run_id"] == "RID"  # added so a caller can fetch the exact result
    # result-only top-level fields are dropped from the gate
    assert "next_steps" not in gate
    assert "usage" not in gate
    assert set(gate["dedup"]) == {"status"}  # dedup harness/reason/warning are result-only
    # core kept verbatim
    assert gate["verdict"] == report.verdict
    assert gate["coverage"] == {"contributed": 2, "failed": 0}
    # per-finding: id is result-only; the agent-facing detail (body/recommendation/sources/line_end)
    # stays in the gate
    f = gate["findings"][0]
    assert "id" not in f
    assert f["body"] and f["recommendation"]
    assert f["location"]["line_end"] == 48
    assert f["sources"][0]["review"] == "r__codex-gpt-5.5"
    assert f["agreement"] == 2


def test_run_gate_keeps_report_fields_verbatim_minus_result_only():
    # The gate's top-level keys are exactly report.json's minus the result-only ones, plus run_id,
    # so adding a Report field without deciding its gate fate fails here (not a silent drop). The
    # kept fields equal the persisted report values verbatim (the "reads against both" guarantee).
    report = _report(findings=[_finding()], next_steps=[NextStepBlock(source="r", steps=["x"])])
    gate = run_gate_dict(report, "RID")
    full = report.model_dump()
    assert set(gate) == (set(full) - {"next_steps", "usage"}) | {"run_id"}
    for key in set(gate) - {"run_id", "dedup", "findings"}:  # verdict, summary, coverage
        assert gate[key] == full[key], key
    assert gate["dedup"] == {"status": full["dedup"]["status"]}  # dedup trimmed to status only
    assert gate["findings"][0] == {k: v for k, v in full["findings"][0].items() if k != "id"}
    # finding-level anti-leak guard (mirrors the top-level one): a new MergedFinding field flows
    # into the gate via model_dump(exclude={"id"}), so pin the exact gate-finding key set. Adding a
    # field without deciding its gate fate fails here. (A model_fields-derived set never fails.)
    assert set(gate["findings"][0]) == {
        "title",
        "body",
        "severity",
        "category",
        "confidence",
        "location",
        "recommendation",
        "sources",
        "agreement",
    }


def test_run_gate_omits_dedup_for_single_review_roster():
    # A roster of one review has nothing to dedup, so the run gate drops `dedup` entirely. The full
    # `dedup` block still lives in report.json / `aeview result` (covered end-to-end in test_cli).
    gate = run_gate_dict(_report(coverage=Coverage(contributed=1, failed=0)), "RID")
    assert "dedup" not in gate


def test_run_gate_omits_dedup_for_single_failed_review():
    # Roster size counts failures: a lone review that FAILED is still a single-review roster
    # (0 contributed + 1 failed), so the gate omits dedup just like a single successful review.
    gate = run_gate_dict(_report(coverage=Coverage(contributed=0, failed=1)), "RID")
    assert "dedup" not in gate


def test_run_gate_keeps_dedup_for_multi_review_roster_including_failures():
    # The trigger is roster size (contributed + failed), not the contributing count: a 2-review
    # roster reports the dedup outcome even when one — or both — of its reviews failed.
    one_failed = _report(coverage=Coverage(contributed=1, failed=1), dedup=Dedup(status="skipped"))
    both_failed = _report(coverage=Coverage(contributed=0, failed=2), dedup=Dedup(status="skipped"))
    assert run_gate_dict(one_failed, "RID")["dedup"] == {"status": "skipped"}
    assert run_gate_dict(both_failed, "RID")["dedup"] == {"status": "skipped"}


def test_render_human_gate_trims_cost_and_dedup_detail():
    # The run gate (gate=True) drops the result-only detail so the human form matches the JSON gate.
    report = _report()  # total cost 0.12
    assert "cost: $0.1200" in render_human(report)  # result/default form shows cost
    assert "cost:" not in render_human(report, gate=True)  # gate hides it
    failed = _report(
        dedup=Dedup(status="failed", harness="dx", reason="timed out", warning="DUPES NOT REMOVED")
    )
    assert "dedup FAILED: DUPES NOT REMOVED" in render_human(failed)  # result shows the reason
    gate_out = render_human(failed, gate=True)
    assert "dedup FAILED" in gate_out and "DUPES NOT REMOVED" not in gate_out  # gate: status only
