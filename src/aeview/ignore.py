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

from dataclasses import replace
from pathlib import Path

from pathspec import GitIgnoreSpec

from .process import run_sync
from .resolve import candidate_rungs
from .scope import ResolvedScope, summarize_diff

IGNORE_FILE = ".aeviewignore"

# (rung directory, its parsed ignore spec), one per `.aeviewignore` on the walk-up, nearest-first.
type RungSpecs = list[tuple[Path, GitIgnoreSpec]]


def _load_specs(cwd: Path) -> RungSpecs:
    """Each rung's `.aeviewignore` (nearest-first), parsed into a spec anchored at the rung. The
    walk is cwd->home (same rungs as reviewer discovery): a `.aeviewignore` in a subdir *below*
    cwd is intentionally not consulted — discovery is upward-only, not gitignore's descend. An
    unreadable file is skipped — filtering must never abort a review."""
    specs: RungSpecs = []
    for rung in candidate_rungs(cwd):
        try:
            text = (rung / IGNORE_FILE).read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        specs.append((rung, GitIgnoreSpec.from_lines(text.splitlines())))
    return specs


def _is_ignored(abs_path: Path, specs: RungSpecs) -> bool:
    """Compose the rung specs gitignore-style: the rung nearest the file that has an opinion wins.
    `specs` is nearest-first, so the first non-None verdict is the nearest one (True = ignore,
    False = negated back in, None = this file has no matching pattern in that rung)."""
    for rung, spec in specs:
        if not abs_path.is_relative_to(rung):
            continue
        decision = spec.check_file(str(abs_path.relative_to(rung))).include
        if decision is not None:
            return decision
    return False


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


def _header_paths(line: str) -> set[str]:
    # `diff --git a/<old> b/<new>` -> {old, new} (best-effort for space-free paths).
    rest = line[len("diff --git ") :]
    marker = rest.rfind(" b/")
    if marker == -1:
        return set()
    old, new = rest[:marker], rest[marker + 3 :].strip()
    return {old[2:] if old.startswith("a/") else old, new}


def _block_paths(block: str) -> set[str]:
    """Every path a diff block touches — both sides of the `diff --git` header, the `---`/`+++`
    lines, and rename/copy from/to. A block is dropped if ANY of these is ignored, so an ignored
    file can't slip through a rename to an allowed path. Only the header region (before the first
    `@@` hunk) is read, so a spoofed `+++ b/...` line in the diff *content* can't redirect the
    match into hiding a file from review."""
    paths: set[str] = set()
    for line in block.splitlines():
        if line.startswith("@@"):
            break  # hunks begin — content lines must never be parsed as headers
        if line.startswith(("+++ ", "--- ")):
            if (p := _strip_ab(line[4:].strip())) is not None:
                paths.add(p)
        elif line.startswith("diff --git "):
            paths |= _header_paths(line)
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            paths.add(line.split(" ", 2)[2].strip())
    return paths


def filter_diff(diff: str, repo_root: Path, specs: RungSpecs) -> tuple[str, list[str]]:
    """Drop diff blocks matching `.aeviewignore`; return (kept diff, sorted ignored paths)."""
    preamble, blocks = _split_blocks(diff)
    kept_blocks: list[str] = []
    ignored: set[str] = set()
    for block in blocks:
        matched = {p for p in _block_paths(block) if _is_ignored(repo_root / p, specs)}
        if matched:
            ignored |= matched
            continue
        kept_blocks.append(block)
    # When every file block is ignored, drop the preamble too (e.g. `git show`'s commit header),
    # so the diff is genuinely empty and the "nothing to review" check fires.
    if blocks and not kept_blocks:
        return "", sorted(ignored)
    return preamble + "".join(kept_blocks), sorted(ignored)


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
    new_scope = replace(resolved, diff=filtered, summary=summarize_diff(filtered))
    return new_scope, ignored
