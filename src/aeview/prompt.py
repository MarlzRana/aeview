"""Compose the prompt handed to a harness: reviewer body + read-only guard + change.

The same (reviewer, bundle) pair always composes the same prompt — deterministic input
is what makes a review reproducible. Inline mode embeds the frozen diff; self-collect
mode embeds a summary plus instructions to inspect the change read-only.
"""

from __future__ import annotations

from pathlib import Path

from .bundle import Bundle
from .resolve import Reviewer

READ_ONLY_GUARD = """\
## Operating rules (read-only)

You are performing a code review. You may read files, grep/ripgrep, and run read-only
git commands (git diff/log/show/status) to gather context. You MUST NOT modify any file,
stage or commit, run mutating or networked commands, execute the code, or run tests. Do
not invoke `aeview` or any nested reviewer. Your only output is the structured review
described by your output schema."""


def compose_prompt(reviewer: Reviewer, bundle: Bundle, full_diff_path: Path | None = None) -> str:
    sections = [_resource_base(reviewer.source), reviewer.body.rstrip(), READ_ONLY_GUARD]
    if bundle.commits.strip():
        sections.append("## Commits in this change\n\n```\n" + bundle.commits.rstrip() + "\n```")
    sections.append(_change_section(bundle, full_diff_path))
    return "\n\n".join(sections)


def _resource_base(source: Path) -> str:
    # Reviewers may keep references/, checklists, or scripts beside their REVIEWER.md and link to
    # them relatively; the harness resolves those against this absolute dir and reads them with its
    # read-only file tools (reads outside cwd are allowed — only writes are blocked). Leads the
    # prompt so the base path is established before the instructions that use it.
    return f"All relative paths in this reviewer's instructions are relative to:\n  {source}"


def _change_section(bundle: Bundle, full_diff_path: Path | None) -> str:
    header = f"Scope: `{bundle.scope.type}`"
    if bundle.scope.base:
        header += f" (base `{bundle.scope.base}`)"

    if bundle.is_inline:
        return (
            f"## Changes under review\n\n{header}\n\n"
            f"The unified diff under review follows. Review only what this diff changes.\n\n"
            f"```diff\n{bundle.diff.rstrip()}\n```"
        )

    inspect = "\n".join(f"- `{cmd}`" for cmd in bundle.inspect) or "- (read the diff file below)"
    path = str(full_diff_path) if full_diff_path else "(see the run's bundle/ directory)"
    return (
        f"## Changes under review (large — inspect it yourself)\n\n{header}\n\n"
        f"This change is too large to inline. Below is a summary; the full unified diff is "
        f"saved at:\n\n  {path}\n\n"
        f"Read that file selectively (Read with offset/limit, or Grep it for the files that "
        f"matter), and/or run these read-only git commands to inspect the live tree:\n\n"
        f"{inspect}\n\n"
        f"Review only what this change touches.\n\n"
        f"### Summary\n\n```\n{bundle.summary}\n```"
    )
