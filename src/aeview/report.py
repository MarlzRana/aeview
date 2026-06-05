"""Render a Report for humans and map it to the exit-code contract.

Exit codes are the loop-until-clean interface:
  0 = approve (no actionable findings)
  1 = needs-attention (findings present)
  2 = error (handled by the CLI, not here)
"""

from __future__ import annotations

from .schema import Report

EXIT_APPROVE = 0
EXIT_NEEDS_ATTENTION = 1
EXIT_ERROR = 2


def exit_code(report: Report) -> int:
    # A run where every review failed has no verdict to trust -> it's an error, not an approve.
    if report.coverage.contributed == 0:
        return EXIT_ERROR
    return EXIT_APPROVE if report.verdict == "approve" else EXIT_NEEDS_ATTENTION


def render_human(report: Report) -> str:
    lines: list[str] = []
    if report.coverage.contributed == 0:
        lines.append(f"[XX] error: {report.summary} (no reviews completed)")
    else:
        mark = "OK" if report.verdict == "approve" else "!!"
        lines.append(f"[{mark}] {report.verdict}: {report.summary}")

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

    if report.usage.cost_usd:
        lines.append(f"     cost: ${report.usage.cost_usd:.4f}")
    return "\n".join(lines)
