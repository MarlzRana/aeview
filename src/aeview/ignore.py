"""`.aeviewignore`: gitignore-style filtering of the resolved diff before it reaches the prompt.

Files matching `.aeviewignore` (e.g. `uv.lock`, `dist/`) never enter the review query. Ignore
files are collected on the same cwd→home walk as reviewers (the home rung is `~/.aeviewignore`,
not a `.aeview/` special case); each file's patterns are anchored at its own directory
(gitignore-faithful), and the rung nearest the changed file wins — so a deeper `!negation` can
re-include something a higher file ignored. Diff paths are repo-root-relative, so they are
resolved against the repo root before matching.

The non-git `patch` scope is left untouched (its paths aren't repo-root-relative). When filtering
removes anything, the scope's `inspect` hints are cleared so a self-collect prompt won't re-derive
the unfiltered diff; a reviewer could still run `git diff` itself under the read-only sandbox — a
residual, accepted leak (this is query-cleanliness, not a security boundary, and reads are allowed
anywhere by design).

Known limitations:
- Merge commits reviewed via `commit:<merge-sha>` produce a combined diff (`diff --cc`), which is
  not split into per-file blocks here, so it passes through unfiltered.
- Paths git renders specially (non-ASCII is handled via `core.quotePath=false`; embedded spaces or
  control chars in the `diff --git` header are best-effort) may not match.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pathspec import GitIgnoreSpec

from .resolve import candidate_rungs
from .scope import ResolvedScope, repo_root, summarize_diff

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


def _header_new_path(line: str) -> str | None:
    # `diff --git a/<old> b/<new>` -> the new (b/) side (best-effort for space-free paths).
    marker = line.rfind(" b/")
    return _strip_ab(line[marker + 1 :].strip()) if marker != -1 else None


def _block_path(block: str) -> str | None:
    """The block's destination (new) path — the only side that decides ignore status: a file
    renamed OUT of an ignored path is still reviewed at its new path, and one renamed INTO an
    ignored path is dropped (a deletion's path comes from the header, which is unchanged). Only
    the header region (before the first `@@` hunk) is read, so a spoofed `+++ b/...` line in the
    diff *content* can't redirect the match into hiding a file from review."""
    plus: str | None = None
    rename_to: str | None = None
    header_new: str | None = None
    for line in block.splitlines():
        if line.startswith("@@"):
            break  # hunks begin — content lines must never be parsed as headers
        if line.startswith("+++ "):
            plus = _strip_ab(line[4:].strip())
        elif line.startswith(("rename to ", "copy to ")):
            rename_to = line.split(" ", 2)[2].strip()
        elif line.startswith("diff --git "):
            header_new = _header_new_path(line)
    return plus or rename_to or header_new


def changed_paths(diff: str) -> list[str]:
    """The destination (new) path of every file block in a unified diff, in order. Reuses the
    same block-splitting + destination-path logic as filtering, so auto-activation matches against
    exactly the files that survived `.aeviewignore` (and ignores a spoofed `+++` content line the
    same way). Blocks whose path can't be parsed (e.g. a `diff --cc` merge block) are skipped."""
    _, blocks = _split_blocks(diff)
    return [p for block in blocks if (p := _block_path(block)) is not None]


def filter_diff(diff: str, root: Path, specs: RungSpecs) -> tuple[str, list[str]]:
    """Drop diff blocks matching `.aeviewignore`; return (kept diff, sorted ignored paths)."""
    preamble, blocks = _split_blocks(diff)
    kept_blocks: list[str] = []
    ignored: set[str] = set()
    for block in blocks:
        rel = _block_path(block)
        if rel is not None and _is_ignored(root / rel, specs):
            ignored.add(rel)
            continue
        kept_blocks.append(block)
    # When every file block is ignored, drop the preamble too (e.g. `git show`'s commit header),
    # so the diff is genuinely empty and the "nothing to review" check fires.
    ignored_paths = sorted(ignored)
    if blocks and not kept_blocks:
        return "", ignored_paths
    return preamble + "".join(kept_blocks), ignored_paths


def filter_resolved(resolved: ResolvedScope, cwd: Path) -> tuple[ResolvedScope, list[str]]:
    """Apply `.aeviewignore` to a resolved scope's diff before bundling. No-op for the non-git
    `patch` scope and when no ignore file or no match applies. Returns the (possibly new) scope
    plus the sorted list of excluded paths (so the caller can surface what was dropped)."""
    if resolved.spec.type == "patch":  # paths aren't repo-root-relative; nothing to anchor against
        return resolved, []
    specs = _load_specs(cwd)
    if not specs:
        return resolved, []
    root = repo_root(cwd)
    if root is None:
        return resolved, []
    filtered, ignored = filter_diff(resolved.diff, root, specs)
    if not ignored:
        return resolved, []
    # Clear the inspect hints too: in self-collect they tell the harness to re-run `git diff`, which
    # re-derives the *unfiltered* diff and re-surfaces ignored files. The frozen diff file is
    # filtered and the commit list rides separately, so the harness still has its context.
    new_scope = replace(resolved, diff=filtered, summary=summarize_diff(filtered), inspect=[])
    return new_scope, ignored
