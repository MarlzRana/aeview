"""Resolve a --scope selector to a concrete diff under review.

Grammar: `--scope <type>[:<value>]`. Bare type runs that scope's default/auto
resolution; `:value` pins a specific one. Some scopes require a value (range, patch).
Omitting --scope entirely is `auto`.

Branch/PR diffs use the merge-base form ("what this branch added"), not 3-dot. We
`fetch` but never `pull`/merge, so a branch that would conflict on merge still diffs
cleanly; only an in-progress, unresolved merge/rebase in the working tree is refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .process import run_sync
from .schema import ScopeSpec

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's well-known empty tree object
UNTRACKED_FILE_CAP = 24 * 1024  # bytes of diff emitted per untracked file

# Scopes that require an explicit :value (no sensible default).
_VALUE_REQUIRED = {"range", "patch"}
_KNOWN_TYPES = {
    "working-tree",
    "staged",
    "branch",
    "pr",
    "effective-pr",
    "commit",
    "range",
    "patch",
    "auto",
}
# --include-dirty folds the working tree onto a committed scope; only meaningful where
# the head is the current HEAD.
_INCLUDE_DIRTY_VALID = {"branch", "auto"}  # fold dirty onto the committed range
_INCLUDE_DIRTY_NOOP = {"working-tree"}  # already dirty -> no effect
_INCLUDE_DIRTY_WIDEN = {"staged"}  # staged is NOT already dirty -> widen to working-tree

# Scopes whose diff incorporates the working tree; only these care about a dirty/conflicted
# checkout. (branch with --include-dirty also reads it — handled via the include_dirty flag.)
_WORKTREE_SCOPES = {"working-tree", "staged", "effective-pr", "auto"}


class ScopeError(Exception):
    """Raised when a scope cannot be resolved, is unsupported, or has no changes."""


@dataclass(slots=True)
class ResolvedScope:
    spec: ScopeSpec
    diff: str
    summary: str
    inspect: list[str] = field(default_factory=list)
    commits: str = ""
    inline_only: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.diff.strip()


def parse_scope(raw: str) -> tuple[str, str | None]:
    """Split `type[:value]` into (type, value). Validates the type and value presence."""
    stype, sep, value = raw.partition(":")
    value = value if sep else None
    if stype not in _KNOWN_TYPES:
        raise ScopeError(f"unknown scope '{stype}'; valid: {', '.join(sorted(_KNOWN_TYPES))}")
    if stype in _VALUE_REQUIRED and not value:
        raise ScopeError(f"scope '{stype}' requires a value, e.g. --scope {stype}:<value>")
    # `range` must be an actual range (A..B / A...B). A bare ref makes `git diff <ref>`
    # compare against the WORKING TREE, which would read uncommitted/conflicted content while
    # the conflict gate (which excludes range) is skipped. Use commit:/branch: for a single ref.
    if stype == "range" and (value is None or ".." not in value):
        raise ScopeError("range scope needs a commit range like A..B or A...B (not a single ref)")
    # A value starting with '-' would be read by git as an option (e.g.
    # `range:--output=x` -> `git diff --output=x` writes a file), so reject it. The bare
    # '-' is allowed: it is the patch-from-stdin sentinel, never passed to git.
    if value is not None and value.startswith("-") and value != "-":
        raise ScopeError(f"scope value may not start with '-': {value!r}")
    return stype, value


# --- git/gh helpers -------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    res = run_sync(["git", "-c", "core.pager=cat", *args], cwd=cwd)
    if res.returncode != 0:
        raise ScopeError(res.stderr.strip() or f"git {' '.join(args)} failed")
    return res.stdout


def _git_ok(args: list[str], cwd: Path) -> bool:
    return run_sync(["git", *args], cwd=cwd).returncode == 0


def _gh(args: list[str], cwd: Path) -> str:
    res = run_sync(["gh", *args], cwd=cwd)
    if res.returncode != 0:
        raise ScopeError(res.stderr.strip() or f"gh {' '.join(args)} failed (is gh installed?)")
    return res.stdout


def _is_repo(cwd: Path) -> bool:
    res = run_sync(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd)
    return res.returncode == 0 and res.stdout.strip() == "true"


def _has_head(cwd: Path) -> bool:
    return _git_ok(["rev-parse", "--verify", "HEAD"], cwd)


def _ref_exists(cwd: Path, ref: str) -> bool:
    return _git_ok(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd)


def _current_branch(cwd: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()


def _is_dirty(cwd: Path) -> bool:
    return bool(_git(["status", "--porcelain"], cwd).strip())


def _merge_base(cwd: Path, base: str) -> str:
    res = run_sync(["git", "merge-base", "HEAD", base], cwd=cwd)
    if res.returncode != 0:
        raise ScopeError(
            f"no common ancestor between HEAD and '{base}' (unrelated histories); "
            f"use --scope range:<A..B> or --scope commit instead"
        )
    return res.stdout.strip()


def _untracked_diff(cwd: Path) -> str:
    listing = _git(["ls-files", "--others", "--exclude-standard", "-z"], cwd)
    parts: list[str] = []
    for rel in (p for p in listing.split("\0") if p):
        res = run_sync(
            ["git", "-c", "core.pager=cat", "diff", "--no-index", "--", "/dev/null", rel], cwd=cwd
        )
        out = res.stdout
        if not out:
            continue
        encoded = out.encode("utf-8")
        if len(encoded) > UNTRACKED_FILE_CAP:
            out = encoded[:UNTRACKED_FILE_CAP].decode("utf-8", "ignore")
            out += f"\n... [untracked {rel} truncated at {UNTRACKED_FILE_CAP} bytes]\n"
        parts.append(out)
    return "".join(parts)


# --- conflict detection ---------------------------------------------------------------


def _in_progress_conflict(cwd: Path) -> str | None:
    """Return a human reason if the working tree has an unresolved merge/rebase."""
    git_dir = Path(_git(["rev-parse", "--git-dir"], cwd).strip())
    if not git_dir.is_absolute():
        git_dir = cwd / git_dir
    if (git_dir / "MERGE_HEAD").exists():
        return "an unfinished merge"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return "an in-progress rebase"
    unmerged = _git(["diff", "--name-only", "--diff-filter=U"], cwd).strip()
    if unmerged:
        return "unmerged paths with conflict markers"
    return None


# --- base resolution ------------------------------------------------------------------


def _pr_base(cwd: Path) -> str | None:
    res = run_sync(["gh", "pr", "view", "--json", "baseRefName"], cwd=cwd)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout).get("baseRefName") or None
    except json.JSONDecodeError:
        return None


def _origin_head(cwd: Path) -> str | None:
    res = run_sync(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd)
    if res.returncode != 0:
        return None
    return res.stdout.strip().removeprefix("refs/remotes/")  # -> "origin/main"


def _first_default(cwd: Path) -> str | None:
    for name in ("main", "master", "trunk"):
        if _ref_exists(cwd, f"origin/{name}"):
            return f"origin/{name}"
        if _ref_exists(cwd, name):
            return name
    return None


def _prefer_remote(cwd: Path, ref: str) -> str:
    if not ref.startswith("origin/") and _ref_exists(cwd, f"origin/{ref}"):
        return f"origin/{ref}"
    return ref


def resolve_base(cwd: Path, explicit: str | None, do_fetch: bool) -> str:
    if explicit:
        ref = explicit
    else:
        pr = _pr_base(cwd)
        ref = (_prefer_remote(cwd, pr) if pr else None) or _origin_head(cwd) or _first_default(cwd)
    if not ref:
        raise ScopeError("could not determine a base branch; pass --scope branch:<ref>")
    if do_fetch:
        branch = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
        # parse_scope rejects values starting with '-', but an `origin/<x>` ref re-splits to
        # `<x>`; guard that segment too so e.g. `origin/-p` can't reach `git fetch` as `--prune`.
        if branch.startswith("-"):
            raise ScopeError(f"invalid base ref segment {branch!r} (looks like a git option)")
        if run_sync(["git", "fetch", "origin", branch], cwd=cwd).returncode == 0:
            ref = _prefer_remote(cwd, branch)
    if not _ref_exists(cwd, ref):
        raise ScopeError(f"base ref '{ref}' does not exist")
    return ref


# --- diff summary ---------------------------------------------------------------------


def summarize_diff(diff: str) -> str:
    """A compact stat-style summary parsed from the unified diff itself (no extra git)."""
    files: dict[str, tuple[int, int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            files.setdefault(current, (0, 0))
        elif line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
            files.setdefault(current, (0, 0))
        elif current and line.startswith("+") and not line.startswith("+++"):
            add, rem = files[current]
            files[current] = (add + 1, rem)
        elif current and line.startswith("-") and not line.startswith("---"):
            add, rem = files[current]
            files[current] = (add, rem + 1)
    if not files:
        return "(no files)"
    rows = [f"  {f}  +{a} -{r}" for f, (a, r) in files.items()]
    return f"{len(files)} file(s) changed:\n" + "\n".join(rows)


# --- per-scope resolution -------------------------------------------------------------


def resolve(
    raw_type: str,
    value: str | None,
    cwd: Path,
    include_dirty: bool = False,
    allow_conflicts: bool = False,
    patch_text: str | None = None,
) -> ResolvedScope:
    if raw_type == "patch":
        return _resolve_patch(value, patch_text)

    if not _is_repo(cwd):
        raise ScopeError(f"{cwd} is not inside a git work tree")

    _validate_include_dirty(raw_type, include_dirty)

    # The conflict gate only matters when the diff reads the working tree. pr/commit/range
    # (and plain branch) diff network/historical refs, so a local in-progress merge is
    # irrelevant to them and must not block the review.
    reads_worktree = raw_type in _WORKTREE_SCOPES or include_dirty
    if reads_worktree and not allow_conflicts and (reason := _in_progress_conflict(cwd)):
        raise ScopeError(
            f"working tree has {reason}; resolve it before review (or pass --allow-conflicts)"
        )

    if raw_type == "auto":
        return _resolve_auto(cwd, include_dirty, allow_conflicts)
    if raw_type == "working-tree":
        return _resolve_working_tree(cwd)
    if raw_type == "staged":
        # --include-dirty widens staged to full working-tree semantics (folds in the
        # unstaged + untracked work the flag name promises, instead of silently dropping it).
        return _resolve_working_tree(cwd) if include_dirty else _resolve_staged(cwd)
    if raw_type == "branch":
        return _resolve_branch(cwd, value, include_dirty)
    if raw_type == "effective-pr":
        return _resolve_effective_pr(cwd, value)
    if raw_type == "pr":
        return _resolve_pr(cwd, value)
    if raw_type == "commit":
        return _resolve_commit(cwd, value)
    if raw_type == "range":
        return _resolve_range(cwd, value)
    raise ScopeError(f"scope '{raw_type}' is not implemented")


def _validate_include_dirty(raw_type: str, include_dirty: bool) -> None:
    allowed = _INCLUDE_DIRTY_VALID | _INCLUDE_DIRTY_NOOP | _INCLUDE_DIRTY_WIDEN
    if not include_dirty or raw_type in allowed:
        return
    raise ScopeError(
        f"--include-dirty is not valid with scope '{raw_type}' "
        f"(use it with branch/auto/staged; it is a no-op on working-tree)"
    )


def _result(
    stype: str, base: str | None, diff: str, inspect: list[str], commits: str = ""
) -> ResolvedScope:
    return ResolvedScope(
        spec=ScopeSpec(type=stype, base=base),
        diff=diff,
        summary=summarize_diff(diff),
        inspect=inspect,
        commits=commits,
    )


def _resolve_working_tree(cwd: Path) -> ResolvedScope:
    # Pre-first-commit (no HEAD): diff the working tree against the empty tree so staged AND
    # unstaged edits to already-added files are captured; `git diff --cached` drops the unstaged.
    base = "HEAD" if _has_head(cwd) else EMPTY_TREE
    tracked = _git(["diff", base], cwd)
    diff = tracked + _untracked_diff(cwd)
    return _result("working-tree", base, diff, [f"git diff {base}"])


def _resolve_staged(cwd: Path) -> ResolvedScope:
    diff = _git(["diff", "--cached"], cwd)
    base = "HEAD" if _has_head(cwd) else EMPTY_TREE
    return _result("staged", base, diff, ["git diff --cached"])


def _resolve_branch(cwd: Path, value: str | None, include_dirty: bool) -> ResolvedScope:
    base_ref = resolve_base(cwd, value, do_fetch=False)
    mb = _merge_base(cwd, base_ref)
    commits = _git(["log", "--oneline", f"{mb}..HEAD"], cwd)
    if include_dirty:
        diff = _git(["diff", mb], cwd) + _untracked_diff(cwd)
        inspect = [f"git diff {mb}"]
    else:
        diff = _git(["diff", f"{mb}..HEAD"], cwd)
        inspect = [f"git diff {mb}..HEAD", f"git log {mb}..HEAD"]
    return _result("branch", base_ref, diff, inspect, commits)


def _resolve_effective_pr(cwd: Path, value: str | None) -> ResolvedScope:
    base_ref = resolve_base(cwd, value, do_fetch=True)
    mb = _merge_base(cwd, base_ref)
    commits = _git(["log", "--oneline", f"{mb}..HEAD"], cwd)
    diff = _git(["diff", mb], cwd) + _untracked_diff(cwd)
    return _result("effective-pr", base_ref, diff, [f"git diff {mb}"], commits)


def _resolve_pr(cwd: Path, value: str | None) -> ResolvedScope:
    args = ["pr", "diff"] + ([value] if value else [])
    diff = _gh(args, cwd)
    base = _pr_base(cwd) if not value else None
    # PR diff is fetched over the network; the read-only sandbox blocks re-fetching, so
    # self-collect must read the frozen diff file rather than re-run gh -> no inspect cmd.
    return _result("pr", base, diff, [])


def _resolve_commit(cwd: Path, value: str | None) -> ResolvedScope:
    ref = value or "HEAD"
    if not _ref_exists(cwd, ref):
        raise ScopeError(f"commit '{ref}' does not exist")
    diff = _git(["show", "--format=medium", ref], cwd)
    return _result("commit", ref, diff, [f"git show {ref}"])


def _resolve_range(cwd: Path, value: str | None) -> ResolvedScope:
    assert value is not None  # parse_scope guarantees a value for range
    diff = _git(["diff", value], cwd)
    return _result("range", value, diff, [f"git diff {value}"])


def _resolve_patch(value: str | None, patch_text: str | None) -> ResolvedScope:
    if patch_text is None:
        raise ScopeError("patch scope requires diff text (file contents or stdin)")
    return ResolvedScope(
        spec=ScopeSpec(type="patch", base=None),
        diff=patch_text,
        summary=summarize_diff(patch_text),
        inspect=[],
        inline_only=True,  # no git to self-collect from
    )


def _resolve_auto(cwd: Path, include_dirty: bool, allow_conflicts: bool) -> ResolvedScope:
    if _is_dirty(cwd):
        return _resolve_working_tree(cwd)
    if _has_head(cwd):
        try:
            base_ref = resolve_base(cwd, None, do_fetch=False)
        except ScopeError:
            base_ref = None
        if base_ref and _current_branch(cwd) != _base_branch_name(base_ref):
            return _resolve_branch(cwd, base_ref, include_dirty)
    raise ScopeError("nothing to review (clean working tree on the default branch)")


def _base_branch_name(base_ref: str) -> str:
    return base_ref.split("/", 1)[1] if base_ref.startswith("origin/") else base_ref
