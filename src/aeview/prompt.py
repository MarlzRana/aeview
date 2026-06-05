"""Compose the prompt handed to a harness: reviewer body + read-only guards + diff.

The same (reviewer, bundle) pair always composes the same prompt — deterministic input
is what makes a review reproducible.
"""

from __future__ import annotations

from .bundle import Bundle
from .resolve import Reviewer

READ_ONLY_GUARD = """\
## Operating rules (read-only)

You are performing a code review. You may read files, grep, and inspect the repository
for context, but you MUST NOT modify any file, run any mutating command, or execute the
code. Do not invoke `aeview` or any nested reviewer. Your only output is the structured
review described by your output schema."""


def compose_prompt(reviewer: Reviewer, bundle: Bundle) -> str:
    return "\n\n".join(
        [
            reviewer.body.rstrip(),
            READ_ONLY_GUARD,
            _changes_section(bundle),
        ]
    )


def _changes_section(bundle: Bundle) -> str:
    return (
        f"## Changes under review\n\n"
        f"Scope: `{bundle.scope.type}` (base `{bundle.scope.base}`)\n\n"
        f"The unified diff under review follows. Review only what this diff changes.\n\n"
        f"```diff\n{bundle.diff.rstrip()}\n```"
    )
