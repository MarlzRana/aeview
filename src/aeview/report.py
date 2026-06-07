"""Render a Report for humans and map it to the exit-code contract.

Exit codes are the loop-until-clean interface:
  0 = approve (no actionable findings)
  1 = needs-attention (findings present)
  2 = error (handled by the CLI, not here)
"""

from __future__ import annotations

from typing import Literal

from .schema import Report

EXIT_APPROVE = 0
EXIT_NEEDS_ATTENTION = 1
EXIT_ERROR = 2

VerdictLabel = Literal["approve", "needs-attention", "error"]

_EXIT_BY_LABEL: dict[VerdictLabel, int] = {
    "approve": EXIT_APPROVE,
    "needs-attention": EXIT_NEEDS_ATTENTION,
    "error": EXIT_ERROR,
}


def report_verdict_label(report: Report) -> VerdictLabel:
    """The trustworthy verdict label. A run with zero contributing reviews has no verdict to
    trust, so it reports `error` (distinct from a real approve/needs-attention). This is the one
    place that owns the contributed==0 rule — shared by exit_code, render_human, and `aeview
    list`, so a threshold change can't drift across them."""
    return "error" if report.coverage.contributed == 0 else report.verdict


def exit_code(report: Report) -> int:
    return _EXIT_BY_LABEL[report_verdict_label(report)]


def render_human(report: Report) -> str:
    lines: list[str] = []
    label = report_verdict_label(report)
    if label == "error":
        lines.append(f"[XX] error: {report.summary} (no reviews completed)")
    else:
        mark = "OK" if label == "approve" else "!!"
        lines.append(f"[{mark}] {label}: {report.summary}")

    if report.coverage.failed:
        lines.append(
            f"     coverage: {report.coverage.contributed} contributed, "
            f"{report.coverage.failed} failed"
        )

    for f in report.findings:
        loc = f"{f.location.file}:{f.location.line_start}"
        if f.location.line_end != f.location.line_start:
            loc += f"-{f.location.line_end}"
        agree = f" (x{f.agreement})" if f.agreement > 1 else ""
        lines.append(f"  - [{f.severity}] {f.title}{agree}")
        lines.append(f"    {loc} :: {f.recommendation}")

    if report.dedup.status == "failed":
        lines.append(f"     dedup FAILED: {report.dedup.warning or report.dedup.reason}")

    if report.usage.total.cost_usd:
        lines.append(f"     cost: ${report.usage.total.cost_usd:.4f}")
    return "\n".join(lines)
