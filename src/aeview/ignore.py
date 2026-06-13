"""`.aeviewignore`: gitignore-style filtering of the resolved diff before it reaches the prompt.

Files matching `.aeviewignore` (e.g. `uv.lock`, `dist/`) never enter the review query. Ignore
files are collected on the same cwd→home walk as reviewers (the home rung is `~/.aeviewignore`,
not a `.aeview/` special case); each file's patterns are anchored at its own directory
(gitignore-faithful), and the rung nearest the changed file wins — so a deeper `!negation` can
re-include something a higher file ignored. Diff paths are repo-root-relative, so they are
resolved against the repo root before matching.

The non-git `patch` scope is left untouched (its paths aren't repo-root-relative). Self-collect's
live `git diff` inspect commands can still surface an ignored file — a known, accepted leak, like
the read-only sandbox allowing reads anywhere.
"""

from __future__ import annotations

from pathlib import Path

from pathspec import GitIgnoreSpec

from .process import run_sync
from .resolve import candidate_rungs
from .scope import ResolvedScope, summarize_diff

IGNORE_FILE = ".aeviewignore"


def _load_specs(cwd: Path) -> list[tuple[Path, GitIgnoreSpec]]:
    """Each rung's `.aeviewignore` (nearest-first), parsed into a spec anchored at the rung. An
    unreadable file is skipped — filtering must never abort a review."""
    specs: list[tuple[Path, GitIgnoreSpec]] = []
    for rung in candidate_rungs(cwd):
        try:
            text = (rung / IGNORE_FILE).read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        specs.append((rung, GitIgnoreSpec.from_lines(text.splitlines())))
    return specs


def _is_ignored(abs_path: Path, specs: list[tuple[Path, GitIgnoreSpec]]) -> bool:
    """Compose the rung specs gitignore-style: the rung nearest the file that has an opinion wins.
    `specs` is nearest-first, so apply farthest-first and keep the last non-None verdict
    (True = ignore, False = negated back in, None = this file has no matching pattern)."""
    verdict: bool | None = None
    for rung, spec in reversed(specs):  # farthest -> nearest, so nearer overrides
        if not abs_path.is_relative_to(rung):
            continue
        decision = spec.check_file(str(abs_path.relative_to(rung))).include
        if decision is not None:
            verdict = decision
    return verdict is True


def _split_blocks(diff: str) -> tuple[str, list[str]]:
    """Split a unified diff into (preamble, per-file blocks). A block starts at `diff --git `; the
    preamble is anything before the first block (e.g. `git show`'s commit header), always kept."""
    blocks: list[str] = []
    preamble: list[str] = []
    current: list[str] | None = None
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current is not None:
                blocks.append("".join(current))
            current = [line]
        elif current is None:
            preamble.append(line)
        else:
            current.append(line)
    if current is not None:
        blocks.append("".join(current))
    return "".join(preamble), blocks


def _strip_ab(token: str) -> str | None:
    if token == "/dev/null":
        return None
    return token[2:] if token.startswith(("a/", "b/")) else token


def _block_path(block: str) -> str | None:
    """The path a diff block concerns — the new (b/) path for adds/modifies/renames, the old (a/)
    path for deletions, falling back to the `diff --git` header for mode/rename-only blocks."""
    new_path: str | None = None
    old_path: str | None = None
    header_path: str | None = None
    for line in block.splitlines():
        if line.startswith("+++ "):
            new_path = _strip_ab(line[4:].strip())
        elif line.startswith("--- "):
            old_path = _strip_ab(line[4:].strip())
        elif line.startswith("diff --git ") and header_path is None:
            # `diff --git a/<path> b/<path>`; take the b/ side (best-effort for space-free paths).
            marker = line.rfind(" b/")
            header_path = line[marker + 3 :].strip() if marker != -1 else None
    return new_path or old_path or header_path


def filter_diff(
    diff: str, repo_root: Path, specs: list[tuple[Path, GitIgnoreSpec]]
) -> tuple[str, list[str]]:
    """Drop diff blocks matching `.aeviewignore`; return (kept diff, ignored paths)."""
    preamble, blocks = _split_blocks(diff)
    kept: list[str] = [preamble] if preamble else []
    ignored: list[str] = []
    for block in blocks:
        rel = _block_path(block)
        if rel is not None and _is_ignored(repo_root / rel, specs):
            ignored.append(rel)
            continue
        kept.append(block)
    return "".join(kept), ignored


def filter_resolved(resolved: ResolvedScope, cwd: Path) -> tuple[ResolvedScope, list[str]]:
    """Apply `.aeviewignore` to a resolved scope's diff before bundling. No-op for the non-git
    `patch` scope and when no ignore file or no match applies. Returns the (possibly new) scope
    plus the sorted list of excluded paths (so the caller can surface what was dropped)."""
    if resolved.spec.type == "patch":  # paths aren't repo-root-relative; nothing to anchor against
        return resolved, []
    specs = _load_specs(cwd)
    if not specs:
        return resolved, []
    res = run_sync(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if res.returncode != 0:
        return resolved, []
    filtered, ignored = filter_diff(resolved.diff, Path(res.stdout.strip()), specs)
    if not ignored:
        return resolved, []
    new_scope = ResolvedScope(
        spec=resolved.spec,
        diff=filtered,
        summary=summarize_diff(filtered),
        inspect=resolved.inspect,
        commits=resolved.commits,
        inline_only=resolved.inline_only,
    )
    return new_scope, sorted(ignored)
