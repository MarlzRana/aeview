"""Resolve the review scope to a concrete git diff.

Increment 1 supports only `working-tree` (tracked changes vs HEAD, plus untracked
files). The full `--scope` grammar (staged/branch/pr/commit/range/patch) arrives in I2.
"""

from __future__ import annotations

from pathlib import Path

from .process import run_sync
from .schema import ScopeSpec

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's well-known empty tree object

SUPPORTED_SCOPES = ("working-tree",)


class ScopeError(Exception):
    """Raised when a scope cannot be resolved or is unsupported."""


def _git(args: list[str], cwd: Path) -> str:
    res = run_sync(["git", "-c", "core.pager=cat", *args], cwd=cwd)
    if res.returncode != 0:
        raise ScopeError(res.stderr.strip() or f"git {' '.join(args)} failed")
    return res.stdout


def _has_head(cwd: Path) -> bool:
    return run_sync(["git", "rev-parse", "--verify", "HEAD"], cwd=cwd).returncode == 0


def _is_git_repo(cwd: Path) -> bool:
    res = run_sync(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd)
    return res.returncode == 0 and res.stdout.strip() == "true"


def collect_diff(scope_type: str, cwd: Path) -> tuple[ScopeSpec, str]:
    """Return the scope spec and the unified diff text for the working tree."""
    if scope_type not in SUPPORTED_SCOPES:
        raise ScopeError(
            f"scope '{scope_type}' is not supported yet; supported: {', '.join(SUPPORTED_SCOPES)}"
        )
    if not _is_git_repo(cwd):
        raise ScopeError(f"{cwd} is not inside a git work tree")

    base = "HEAD" if _has_head(cwd) else EMPTY_TREE
    tracked = _git(["diff", base], cwd=cwd)
    untracked = _untracked_diff(cwd)
    diff = tracked + untracked
    return ScopeSpec(type="working-tree", base=base), diff


def _untracked_diff(cwd: Path) -> str:
    listing = _git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=cwd)
    parts: list[str] = []
    for rel in (p for p in listing.split("\0") if p):
        # Render each untracked file as an add-from-empty diff so reviewers see new files.
        res = run_sync(
            ["git", "-c", "core.pager=cat", "diff", "--no-index", "--", "/dev/null", rel],
            cwd=cwd,
        )
        # --no-index exits 1 when files differ; that is expected, not an error.
        if res.stdout:
            parts.append(res.stdout)
    return "".join(parts)
