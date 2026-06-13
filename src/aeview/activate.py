"""Auto mode: pick reviewers whose `auto-activate-paths` match the changed files.

A bare `aeview run` (no --reviewers) runs the `default` reviewer plus any reviewer whose
frontmatter `auto-activate-paths` globs match a file in the (already .aeviewignore-filtered)
diff. Matching is literal-glob via `PurePath.full_match`, NOT the gitignore engine `.aeviewignore`
uses: a single `*` does not cross a `/`, `**` crosses directory boundaries, there is no negation,
and a bare directory name matches nothing (write `dir/**`). Case-sensitive, so a reviewer
activates identically on macOS and Linux rather than following the platform default.

Each reviewer is anchored at its scope path (the parent of its `.aeview` dir): a changed file is
considered only if it lives under that path, and is matched relative to it. There is no clamp — a
home reviewer (`~/.aeview/reviewers/...`) anchored at `~` only reaches repos under home, and needs
`**/...` globs to match inside them. Discovery is upward-only (cwd->home), so a reviewer in a
subdirectory below cwd is never consulted.
"""

from __future__ import annotations

from pathlib import Path

from .ignore import changed_paths
from .resolve import (
    REVIEWER_FILE,
    ResolveError,
    discover_reviewer_sources,
    parse_reviewer,
    reviewer_scope_path,
)


def select_auto_reviewers(cwd: Path, root: Path, diff: str) -> list[str]:
    """Reviewer names auto-activated by the diff, in nearest-first discovery order. `root` is the
    repo top-level that makes the repo-root-relative diff paths absolute; `diff` is already
    .aeviewignore-filtered, so an ignored file never triggers activation. `default` runs
    unconditionally and is added by the caller, not here."""
    abs_changed = [root / rel for rel in changed_paths(diff)]
    activated: list[str] = []
    for disc in discover_reviewer_sources(cwd):
        try:
            front, _ = parse_reviewer(disc.source / REVIEWER_FILE)
        except ResolveError:
            continue  # unparseable frontmatter can't declare globs, so it never auto-activates
        globs = front.auto_activate_paths
        if globs and _any_match(abs_changed, reviewer_scope_path(disc.source), globs):
            activated.append(disc.name)
    return activated


def _any_match(abs_paths: list[Path], scope_path: Path, globs: list[str]) -> bool:
    """True if any changed file under `scope_path` full-matches any glob (relative to scope_path).
    Literal-glob via PurePath.full_match: `*` stops at `/`, `**` crosses, case-sensitive."""
    for abs_path in abs_paths:
        if not abs_path.is_relative_to(scope_path):
            continue  # outside this reviewer's tree -> never its concern (no clamp the other way)
        rel = abs_path.relative_to(scope_path)
        if any(rel.full_match(glob, case_sensitive=True) for glob in globs):
            return True
    return False
