"""aeview command-line interface.

Increment 1 wires the vertical slice: `aeview run --scope working-tree` resolves the
default reviewer, bundles the working-tree diff, fans it across the configured harness,
merges, and writes report.json with a 0/1/2 exit code. `version` reports the build.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .activate import select_auto_reviewers
from .bundle import Bundle, build_bundle
from .config import HarnessInstance, Settings, ensure_seeded, load_settings
from .doctor import run_doctor
from .fanout import fan_out
from .ignore import filter_resolved
from .merge import merge_reviews
from .prompt import compose_prompt
from .report import EXIT_APPROVE, EXIT_ERROR, exit_code, render_human, report_verdict_label
from .resolve import (
    RESERVED_REVIEWER_NAMES,
    DiscoveredReviewer,
    ResolveError,
    Reviewer,
    build_roster,
    discover_reviewer_sources,
    discover_reviewers,
    resolve_reviewer,
)
from .runstore import (
    RunStore,
    atomic_write_text,
    effective_overall,
    latest_run_id,
    list_manifests,
    new_run_id,
    now_iso,
    prune_runs,
    reconcile_interrupted,
)
from .schema import DedupPlan, Invocation, Report, RosterEntry, RunManifest, ScopeSpec
from .scope import ResolvedScope, ScopeError, parse_scope, repo_root
from .scope import resolve as resolve_scope

app = typer.Typer(
    name="aeview",
    help="Fan code reviewers across agent harnesses and merge one verdict.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    # Eager so `aeview --version` prints and exits before any subcommand is required.
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _main(
    _version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Print the version."
        ),
    ] = False,
) -> None:
    """Seed ~/.aeview if needed, then dispatch the subcommand."""
    ensure_seeded()


@app.command()
def version() -> None:
    """Print the aeview version."""
    typer.echo(__version__)


_DOCTOR_MARK = {"ok": "OK", "warn": "--", "fail": "XX"}


@app.command()
def doctor() -> None:
    """Preflight: reviewer config, harness binaries + auth, and gh. Exits 1 if anything fails."""
    report = run_doctor(Path.cwd(), load_settings())
    for check in report.checks:
        typer.echo(f"[{_DOCTOR_MARK[check.status]}] {check.name}: {check.detail}")
    raise typer.Exit(0 if report.ok else 1)


@app.command()
def run(
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="What to review: <type>[:value]. Types: working-tree, staged, branch, "
            "pr, effective-pr, commit, range, patch; omitted -> auto.",
        ),
    ] = "auto",
    reviewers: Annotated[
        list[str] | None,
        typer.Option(
            "--reviewers",
            help="Reviewer names: comma-separated (--reviewers a,b) or repeated "
            "(--reviewers a --reviewers b). Use 'all' for every reviewer found here.",
        ),
    ] = None,
    include_dirty: Annotated[
        bool,
        typer.Option("--include-dirty", help="Fold uncommitted work onto a committed scope."),
    ] = False,
    allow_conflicts: Annotated[
        bool,
        typer.Option("--allow-conflicts", help="Review despite an in-progress merge/rebase."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Preview roster, scope, and bundle size; make no model calls, persist nothing.",
        ),
    ] = False,
    output: Annotated[
        Path | None, typer.Option("--output", help="Also write report.json to this path.")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Print report.json instead of the human summary.")
    ] = False,
) -> None:
    """Run reviewers over a scope and emit a merged report."""
    cwd = Path.cwd()
    settings = load_settings()
    try:
        names = _split_reviewers(reviewers)
        stype, value = parse_scope(scope)
        patch_text = _read_patch(value) if stype == "patch" else None
        plan = _plan_run(
            names, stype, value, cwd, include_dirty, allow_conflicts, patch_text, settings
        )
    except (ScopeError, ResolveError) as exc:
        typer.echo(f"aeview: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc

    if dry_run:
        typer.echo(_render_dry_run(plan, settings))
        raise typer.Exit(EXIT_APPROVE)

    if plan.ignored:  # surface what .aeviewignore dropped — never silently
        typer.echo(f"aeview: excluded {len(plan.ignored)} file(s) via .aeviewignore", err=True)
    if plan.auto_activated:  # surface which reviewers the changed paths pulled in — never silently
        joined = ", ".join(plan.auto_activated)
        typer.echo(
            f"aeview: auto-activated {len(plan.auto_activated)} reviewer(s): {joined}", err=True
        )

    reconcile_interrupted()  # crashed 'running' runs -> 'interrupted' so prune can collect them
    prune_runs(settings.retention)  # keep ~/.aeview/runs bounded — only ever on a real `run`
    report = asyncio.run(_execute(plan, settings, cwd))

    rendered = json.dumps(report.model_dump(), indent=2) if json_out else render_human(report)
    typer.echo(rendered)
    if output is not None:
        output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    raise typer.Exit(exit_code(report))


def _split_reviewers(values: list[str] | None) -> list[str] | None:
    """Flatten --reviewers: comma-separated (a,b) and/or repeated (--reviewers a --reviewers b).

    Returns None when --reviewers is omitted entirely — the signal for auto mode (run `default`
    plus every reviewer whose auto-activate-paths match). Any explicit value disables auto mode,
    so `--reviewers default` runs only the default. Passing it *blank* (e.g. `--reviewers ""`,
    usually an empty shell variable) is a mistake and errors rather than silently defaulting.
    """
    if values is None:
        return None
    names = [n.strip() for item in values for n in item.split(",") if n.strip()]
    if not names:
        raise ResolveError(
            "--reviewers was given but empty; omit it for auto mode "
            "(default + path-activated reviewers)"
        )
    return names


def _resolve_all_lenient(names: list[str], cwd: Path, settings: Settings) -> list[Reviewer]:
    """Resolve discovered reviewers for `--reviewers all`, skipping (with a warning) any that
    have invalid config, so one broken reviewer doesn't abort the whole bulk run."""
    resolved: list[Reviewer] = []
    for name in names:
        try:
            resolved.append(resolve_reviewer(name, cwd, settings))
        except ResolveError as exc:
            typer.echo(f"aeview: skipping reviewer '{name}': {exc}", err=True)
    return resolved


def _read_patch(value: str | None) -> str:
    if value == "-":
        return sys.stdin.read()
    if not value:
        raise ScopeError("patch scope requires --scope patch:<file> or patch:-")
    path = Path(value)
    if not path.is_file():
        raise ScopeError(f"patch file not found: {value}")
    return path.read_text(encoding="utf-8")


def _dedup_plan(roster: list[RosterEntry], settings: Settings) -> DedupPlan | None:
    """Pin the dedup harness in run.json when the roster will need it (>1 review).

    Recording it freezes which harness this run used against later settings.json edits — the
    same reason the roster and bundle are frozen. Null when roster=1 (dedup can't run) or no
    harness is configured (the run then surfaces that as dedup.status=failed at merge time)."""
    instance = settings.deduplication_harness
    if len(roster) <= 1 or instance is None:
        return None
    return DedupPlan(
        id=instance.descriptor_id,
        harness=instance.harness,
        model=instance.model,
        thinking=instance.thinking,
    )


@dataclass(slots=True)
class _Plan:
    """The resolved, side-effect-free run plan shared by a real run and `--dry-run`."""

    names: list[str]
    reviewers: list[Reviewer]
    roster: list[RosterEntry]
    bundle: Bundle
    ignored: list[str]  # paths excluded by .aeviewignore (surfaced; never silently dropped)
    auto_activated: list[str]  # reviewers auto mode added beyond default (empty otherwise)


def _plan_run(
    names: list[str] | None,
    stype: str,
    value: str | None,
    cwd: Path,
    include_dirty: bool,
    allow_conflicts: bool,
    patch_text: str | None,
    settings: Settings,
) -> _Plan:
    """Resolve reviewers + scope and build the bundle — the sync front half shared by `run` and
    `--dry-run`. Raises ScopeError/ResolveError; makes no model calls and writes no run dir.

    Scope is resolved + .aeviewignore-filtered first because auto mode (names is None) selects
    reviewers from the changed files; the same order means a scope error surfaces ahead of a
    reviewer error in every mode.
    """
    resolved = resolve_scope(stype, value, cwd, include_dirty, allow_conflicts, patch_text)
    if resolved.is_empty:
        raise ScopeError(f"nothing to review for scope '{stype}'")
    # Drop .aeviewignore'd files before measuring/bundling, so the byte count, the prompt, and
    # auto-activation only see what's actually under review.
    resolved, ignored = filter_resolved(resolved, cwd)
    if resolved.is_empty:
        raise ScopeError(
            f"nothing to review for scope '{stype}' (all changes matched .aeviewignore)"
        )

    reviewers, auto_activated = _select_reviewers(names, resolved, cwd, settings)
    roster = build_roster(reviewers)
    if not roster:
        raise ResolveError(
            "no harnesses resolved (check frontmatter harnesses / fallbackReviewerHarnesses)"
        )
    return _Plan(
        names=[r.name for r in reviewers],  # resolve_reviewer pins name == dir == frontmatter name
        reviewers=reviewers,
        roster=roster,
        bundle=build_bundle(resolved),
        ignored=ignored,
        auto_activated=auto_activated,
    )


def _select_reviewers(
    names: list[str] | None, resolved: ResolvedScope, cwd: Path, settings: Settings
) -> tuple[list[Reviewer], list[str]]:
    """Pick the reviewer set + the auto-activated subset. None = auto mode; `all` = every discovered
    reviewer; otherwise the explicit names, resolved fail-fast (you asked for these specific ones).
    Only auto mode auto-activates; the other modes report an empty auto-activated list."""
    if names is None:
        return _select_auto(resolved, cwd, settings)
    if "all" in names:
        discovered = discover_reviewers(cwd)
        if not discovered:
            raise ResolveError("no reviewers found via the walk-up from this directory")
        # `all` is a bulk request: one mis-configured reviewer shouldn't abort the rest.
        reviewers = _resolve_all_lenient(discovered, cwd, settings)
        if not reviewers:
            raise ResolveError("every discovered reviewer had invalid config")
        return reviewers, []
    names = list(dict.fromkeys(names))  # de-dupe, preserve order
    return [resolve_reviewer(name, cwd, settings) for name in names], []


def _select_auto(
    resolved: ResolvedScope, cwd: Path, settings: Settings
) -> tuple[list[Reviewer], list[str]]:
    """Auto mode: always run `default` (fail-fast — the package-seeded baseline; a broken default
    is a real problem), plus every reviewer whose auto-activate-paths match a changed file (lenient
    skip-with-warning, like `all`). Returns (reviewers, auto-activated names) — the names are only
    the path-matched extras that actually resolved and run."""
    default = resolve_reviewer("default", cwd, settings)
    if resolved.spec.type == "patch":
        return [default], []  # patch paths aren't repo-root-relative; nothing to anchor against
    root = repo_root(cwd)
    if root is None:
        return [default], []  # not a git work tree; no root to anchor the changed paths against
    # Drop `default` from the matches: it's already resolved unconditionally, and resolving it a
    # second time would build a duplicate roster entry with the same id -> clobbered review files.
    matched = [n for n in select_auto_reviewers(cwd, root, resolved.diff) if n != "default"]
    extra_reviewers = _resolve_all_lenient(matched, cwd, settings)
    return [default, *extra_reviewers], [r.name for r in extra_reviewers]


async def _execute(plan: _Plan, settings: Settings, cwd: Path) -> Report:
    store = RunStore.create(new_run_id())
    typer.echo(f"run {store.run_id}", err=True)

    manifest = RunManifest(
        run_id=store.run_id,
        created_at=now_iso(),
        started_at=now_iso(),
        overall="running",
        invocation=Invocation(reviewers=plan.names, scope=plan.bundle.scope),
        roster=plan.roster,
        dedup=_dedup_plan(plan.roster, settings),
        cwd=cwd,  # resume re-runs from here, not the caller's cwd
        pid=os.getpid(),  # recorded so liveness can tell a live run from a crash
    )
    store.write_manifest(manifest)
    full_diff_path = store.write_bundle(plan.bundle)

    prompt_by_reviewer = {
        r.name: compose_prompt(r, plan.bundle, full_diff_path) for r in plan.reviewers
    }
    for reviewer_name, prompt in prompt_by_reviewer.items():
        store.write_prompt(reviewer_name, prompt)

    return await _run_reviews_and_merge(
        store,
        manifest,
        plan.roster,
        prompt_by_reviewer,
        cwd,
        settings.review_timeout_seconds,
        settings.harness_binaries,
    )


def _merge_settings(dedup: DedupPlan | None, harness_binaries: dict[str, str]) -> Settings:
    """Settings carrying the run's *pinned* dedup harness (so a re-merge uses the harness frozen in
    run.json, never whatever settings.json says now) plus the live harnessBinaries overrides — a
    binary path is an install/env concern read live, not part of the pinned review identity."""
    if dedup is None:
        return Settings(deduplication_harness=None, harness_binaries=harness_binaries)
    instance = HarnessInstance(harness=dedup.harness, model=dedup.model, thinking=dedup.thinking)
    return Settings(deduplication_harness=instance, harness_binaries=harness_binaries)


async def _run_reviews_and_merge(
    store: RunStore,
    manifest: RunManifest,
    entries: list[RosterEntry],
    prompt_by_reviewer: dict[str, str],
    cwd: Path,
    timeout: float | None,
    harness_binaries: dict[str, str],
) -> Report:
    """Run the given roster entries (fresh run = all; resume = the non-done subset), then merge
    *all* on-disk reviews. The shared core of `run` and `resume`: it reads the persisted prompts
    + frozen bundle and re-merges via the run.json-pinned dedup plan, so completion truth comes
    from the run dir, not the in-memory plan. `harness_binaries` (live) feeds the binary override
    to both the review fan-out and the dedup harness."""
    if entries:
        await fan_out(store, entries, prompt_by_reviewer, cwd, timeout, harness_binaries)
    report = await merge_reviews(
        store.read_reviews(), _merge_settings(manifest.dedup, harness_binaries), store, cwd
    )
    store.write_report(report)
    manifest.overall = "failed" if report.coverage.contributed == 0 else "done"
    manifest.finished_at = now_iso()
    store.write_manifest(manifest)
    return report


def _render_dry_run(plan: _Plan, settings: Settings) -> str:
    bundle = plan.bundle
    mode = "inline" if bundle.is_inline else "self-collect"
    ignored_display = ", ".join(plan.ignored) or "—"
    activated_display = ", ".join(plan.auto_activated) or "—"
    lines = [
        "dry run — no model calls, nothing persisted",
        f"scope: {_scope_label(bundle.scope)}",
        f"bundle: {mode}, {bundle.diff_bytes} bytes",
        f"ignored ({len(plan.ignored)} via .aeviewignore): {ignored_display}",
        f"auto-activated ({len(plan.auto_activated)} via auto-activate-paths): {activated_display}",
        f"reviewers: {', '.join(plan.names)}",
        f"roster ({len(plan.roster)} review{'' if len(plan.roster) == 1 else 's'}):",
    ]
    for entry in plan.roster:
        thinking = f" thinking={entry.thinking}" if entry.thinking else ""
        lines.append(f"  - {entry.id}  ({entry.harness} {entry.model}{thinking})")
    dedup = _dedup_plan(plan.roster, settings)
    if dedup is not None:
        lines.append(f"dedup: {dedup.harness} {dedup.model}")
    elif len(plan.roster) <= 1:
        lines.append("dedup: skipped (single review)")
    else:
        lines.append("dedup: not configured (findings pass through as a raw union)")
    return "\n".join(lines)


# --- query commands (read the run dir; never mutate it) ---

_REVIEW_STATUS_ORDER = ("done", "running", "pending", "failed")


def _scope_label(scope: ScopeSpec) -> str:
    return f"{scope.type} (base {scope.base})" if scope.base else scope.type


def _load_manifest_or_exit(run_id: str | None) -> tuple[str, RunManifest]:
    """Resolve a run-id (or the latest run) to (dir-id, manifest), or exit 2 with a clear message.

    Returns the run *directory* name as the authoritative id; callers read all of a run's files
    via this id, never via the manifest's self-declared run_id (which a corrupt run.json could
    point elsewhere). A user-supplied id is rejected if it isn't a plain run-dir name, so a read
    command can't escape ~/.aeview/runs — the same dir-is-authoritative invariant prune relies on.
    """
    # Distinguish "omitted" (None -> latest) from "given but empty" ('' -> a mistake, not the
    # latest run): otherwise `aeview result "$RUN_ID"` with an empty var silently reads the
    # latest run and returns its verdict, which automation would trust.
    if run_id is None:
        rid = latest_run_id()
        if rid is None:
            typer.echo("aeview: no runs found", err=True)
            raise typer.Exit(EXIT_ERROR)
    else:
        rid = run_id
    if rid in {"", ".", ".."} or "/" in rid or "\\" in rid:
        typer.echo(f"aeview: run '{rid}' not found", err=True)
        raise typer.Exit(EXIT_ERROR)
    try:
        return rid, RunStore(rid).read_manifest()
    except (OSError, ValueError) as exc:
        typer.echo(f"aeview: run '{rid}' not found", err=True)
        raise typer.Exit(EXIT_ERROR) from exc


_WAIT_POLL_S = 1.0


def _terminal_exit_code(rid: str) -> int:
    # A finished run carries its verdict in report.json; a run that ended without one
    # (interrupted/failed before merge) is an error.
    try:
        return exit_code(RunStore(rid).read_report())
    except OSError, ValueError:
        return EXIT_ERROR


def _render_status(rid: str, manifest: RunManifest, json_out: bool) -> None:
    reviews = {r.id: r for r in RunStore(rid).read_reviews()}
    rows = [
        {"id": e.id, "status": reviews[e.id].status if e.id in reviews else "pending"}
        for e in manifest.roster
    ]
    counts = Counter(r["status"] for r in rows)
    state = effective_overall(manifest)  # crashed 'running' (dead pid) shows as 'interrupted'
    if json_out:
        typer.echo(
            json.dumps(
                {
                    "run_id": rid,
                    "overall": state,
                    "created_at": manifest.created_at,
                    "started_at": manifest.started_at,
                    "finished_at": manifest.finished_at,
                    "scope": manifest.invocation.scope.model_dump(),
                    "counts": dict(counts),
                    "reviews": rows,
                },
                indent=2,
            )
        )
        return
    typer.echo(f"run {rid}")
    typer.echo(f"state: {state}")
    typer.echo(f"scope: {_scope_label(manifest.invocation.scope)}")
    parts = [f"{counts[s]} {s}" for s in _REVIEW_STATUS_ORDER if counts.get(s)]
    typer.echo(f"reviews: {', '.join(parts) or '0'} (of {len(rows)})")
    for r in rows:
        typer.echo(f"  [{r['status']}] {r['id']}")


@app.command()
def status(
    run_id: Annotated[
        str | None, typer.Argument(help="Run id; defaults to the latest run.")
    ] = None,
    wait: Annotated[
        bool,
        typer.Option(
            "--wait",
            help="Block until the run reaches a terminal state, then exit with its 0/1/2 verdict.",
        ),
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")] = False,
) -> None:
    """Show a run's progress: per-review state + coverage (defaults to the latest run)."""
    rid, manifest = _load_manifest_or_exit(run_id)
    if not wait:
        _render_status(rid, manifest, json_out)
        return
    # Poll to a terminal state. effective_overall folds in liveness, so a crashed 'running' run
    # (dead pid) reads terminal and we stop instead of polling forever.
    while effective_overall(manifest) == "running":
        time.sleep(_WAIT_POLL_S)
        try:
            manifest = RunStore(rid).read_manifest()
        except (OSError, ValueError) as exc:
            # The run dir vanished mid-wait (a concurrent prune, or the user deleted it).
            typer.echo(f"aeview: run '{rid}' is gone", err=True)
            raise typer.Exit(EXIT_ERROR) from exc
    _render_status(rid, manifest, json_out)
    raise typer.Exit(_terminal_exit_code(rid))


@app.command()
def result(
    run_id: Annotated[
        str | None, typer.Argument(help="Run id; defaults to the latest run.")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")] = False,
) -> None:
    """Print the merged report for a finished run; exits with that run's 0/1/2 verdict code."""
    rid, manifest = _load_manifest_or_exit(run_id)
    try:
        report = RunStore(rid).read_report()
    except (OSError, ValueError) as exc:
        typer.echo(
            f"aeview: run '{rid}' has no report yet (state: {effective_overall(manifest)}); "
            f"see `aeview status {rid}`",
            err=True,
        )
        raise typer.Exit(EXIT_ERROR) from exc
    _emit_report(report, json_out)


def _emit_report(report: Report, json_out: bool) -> None:
    """Print a report (human or JSON) and exit with its 0/1/2 verdict code — the shared tail of
    `result` and `resume`."""
    rendered = json.dumps(report.model_dump(), indent=2) if json_out else render_human(report)
    typer.echo(rendered)
    raise typer.Exit(exit_code(report))


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Run id to resume.")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")] = False,
) -> None:
    """Re-run a run's non-`done` reviews against its frozen bundle, then re-merge."""
    rid, manifest = _load_manifest_or_exit(run_id)
    store = RunStore(rid)
    # Refuse to double-run a live run (e.g. a foreground run going in another terminal): only a
    # finished or crashed run is safe to take over. effective_overall is "running" only when the
    # pid is alive. (Two *simultaneous* resumes of the same crashed run is a narrow unguarded gap
    # — a run-dir lock is a deferred stretch item; see the roadmap.)
    if effective_overall(manifest) == "running":
        typer.echo(
            f"aeview: run '{rid}' is still running; wait for it or kill it before resuming",
            err=True,
        )
        raise typer.Exit(EXIT_ERROR)

    done_ids = {r.id for r in store.read_reviews() if r.status == "done"}
    pending = [e for e in manifest.roster if e.id not in done_ids]
    if not pending:
        # All reviews are done. If the run already merged, it's complete — show it and stop;
        # otherwise (crashed before merge) fall through to a merge-only resume.
        try:
            report = store.read_report()
        except OSError, ValueError:
            pass
        else:
            typer.echo(f"aeview: run '{rid}' already complete; nothing to resume", err=True)
            _emit_report(report, json_out)

    # Read each reviewer's frozen prompt once (a reviewer may have several pending instances).
    try:
        prompts = {r: store.read_prompt(r) for r in dict.fromkeys(e.reviewer for e in pending)}
    except OSError as exc:
        typer.echo(f"aeview: cannot resume run '{rid}': {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc

    # Take ownership: mark running under this process so liveness tracks it; clear the old finish.
    # Drop the stale report so a crash mid-resume can't leave `result`/`status --wait` returning
    # the old run's verdict — a fresh report is written when the re-merge completes. Re-run from
    # the original repo (manifest.cwd) so a self-collect harness inspects the right tree.
    cwd = manifest.cwd or Path.cwd()
    store.clear_report()
    manifest.overall = "running"
    manifest.finished_at = None
    manifest.pid = os.getpid()
    store.write_manifest(manifest)

    settings = load_settings()
    report = asyncio.run(
        _run_reviews_and_merge(
            store,
            manifest,
            pending,
            prompts,
            cwd,
            settings.review_timeout_seconds,
            settings.harness_binaries,
        )
    )
    _emit_report(report, json_out)


def _run_row(manifest: RunManifest) -> dict:
    """One `list` row: verdict + coverage come from report.json when the run has one; a
    still-running or report-less run shows its run-state (liveness-adjusted) instead."""
    state = effective_overall(manifest)  # a crashed 'running' run shows as 'interrupted'
    verdict: str = state
    coverage: dict | None = None
    if state != "running":
        try:
            report = RunStore(manifest.run_id).read_report()
        except OSError, ValueError:
            report = None
        if report is not None:
            verdict = report_verdict_label(report)  # shares the contributed==0 rule with result
            coverage = {
                "contributed": report.coverage.contributed,
                "failed": report.coverage.failed,
            }
    return {
        "run_id": manifest.run_id,
        "created_at": manifest.created_at,
        "scope": _scope_label(manifest.invocation.scope),
        "verdict": verdict,
        "coverage": coverage,
    }


@app.command("list")
def list_runs(
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")] = False,
) -> None:
    """List recent runs newest-first: id, time, scope, verdict, coverage."""
    rows = [_run_row(m) for m in list_manifests()]
    if json_out:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("no runs")
        return
    for r in rows:
        cov = r["coverage"]
        cov_str = (
            "-" if cov is None else f"{cov['contributed']} contributed, {cov['failed']} failed"
        )
        typer.echo(f"{r['run_id']}  {r['created_at']}  {r['scope']}  {r['verdict']}  {cov_str}")


# --- reviewers (introspect the walk-up) ---


def _display_path(path: Path) -> str:
    """Show a reviewer dir with the home prefix collapsed to `~` (repo paths stay absolute),
    so the source reads as repo-vs-global at a glance. Uses path semantics (not a string prefix)
    so a sibling like /home/foobar isn't mis-collapsed under home /home/foo."""
    home = Path.home()
    if path == home:
        return "~"
    try:
        return f"~/{path.relative_to(home).as_posix()}"
    except ValueError:
        return str(path)


def _reviewer_row(discovered: DiscoveredReviewer, cwd: Path, settings: Settings) -> dict:
    row: dict = {
        "name": discovered.name,
        "source": _display_path(discovered.source),
        "shadows": [_display_path(p) for p in discovered.shadowed],
    }
    try:
        reviewer = resolve_reviewer(discovered.name, cwd, settings)
    except ResolveError as exc:
        row.update(ok=False, error=str(exc), description="", harnesses=[])
        return row
    row.update(
        ok=True,
        error=None,
        description=reviewer.description,
        harnesses=[ref.id for ref in reviewer.harnesses],
    )
    return row


def _reviewer_detail(reviewer: Reviewer) -> dict:
    return {
        "name": reviewer.name,
        "source": _display_path(reviewer.source),
        "description": reviewer.description,
        "harnesses": [
            {
                "id": ref.id,
                "harness": ref.instance.harness,
                "model": ref.instance.model,
                "thinking": ref.instance.thinking,
            }
            for ref in reviewer.harnesses
        ],
    }


def _render_reviewer_detail(detail: dict) -> str:
    lines = [
        f"reviewer: {detail['name']}",
        f"source: {detail['source']}",
        f"description: {detail['description']}",
        "harnesses:",
    ]
    for h in detail["harnesses"]:
        thinking = f" thinking={h['thinking']}" if h["thinking"] else ""
        lines.append(f"  - {h['id']}  ({h['harness']} {h['model']}{thinking})")
    return "\n".join(lines)


@app.command()
def reviewers(
    name: Annotated[
        str | None, typer.Argument(help="Show detail for one reviewer instead of listing all.")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")] = False,
) -> None:
    """List the reviewers available here (walk-up, first-match-wins) or show one's detail."""
    cwd = Path.cwd()
    settings = load_settings()
    if name is not None:
        try:
            reviewer = resolve_reviewer(name, cwd, settings)
        except ResolveError as exc:
            typer.echo(f"aeview: {exc}", err=True)
            raise typer.Exit(EXIT_ERROR) from exc
        detail = _reviewer_detail(reviewer)
        typer.echo(json.dumps(detail, indent=2) if json_out else _render_reviewer_detail(detail))
        return

    rows = [_reviewer_row(d, cwd, settings) for d in discover_reviewer_sources(cwd)]
    if json_out:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("no reviewers found")
        return
    for r in rows:
        header = f"{r['name']} — {r['description']}" if r["description"] else r["name"]
        typer.echo(header)
        typer.echo(f"  source: {r['source']}")
        if r["ok"]:
            typer.echo(f"  harnesses: {', '.join(r['harnesses']) or '(none)'}")
        else:
            typer.echo(f"  INVALID: {r['error']}")
        if r["shadows"]:
            typer.echo(f"  shadows: {', '.join(r['shadows'])}")


# --- init (scaffold a repo reviewer) ---

# fullmatch (not match): re's `$` also matches before a trailing newline, so `match` would let
# a name like "foo\n" through and create a newline-bearing reviewer dir.
_SAFE_REVIEWER_NAME = re.compile(r"[A-Za-z0-9_-]+")
_STARTER_HARNESS_BLOCK = "harnesses:\n  - { harness: claude-code, model: claude-opus-4-8 }\n"


def _starter_reviewer(name: str, with_harness: bool) -> str:
    return (
        f"---\n"
        # Quote the name: an unquoted YAML 1.1 scalar like `yes`/`no`/`null` parses as a
        # bool/None, so `init yes` would scaffold a reviewer whose frontmatter name isn't "yes".
        f'name: "{name}"\n'
        f"description: TODO one-line summary of what this reviewer checks.\n"
        f"{_STARTER_HARNESS_BLOCK if with_harness else ''}"
        f"---\n\n"
        f"You are a focused code reviewer. TODO: describe the specific class of problems this\n"
        f"reviewer should hunt for in the change under review.\n\n"
        f"## Focus\n\n"
        f"- TODO: the concrete things to look for.\n\n"
        f"## Grounding\n\n"
        f"- Every finding cites a real file and line range from the change under review.\n"
        f"- `recommendation` is a concrete, specific fix; do not invent code not in the diff.\n"
        f"- Set `confidence` honestly: 1.0 only when you can point at the failing line.\n\n"
        f"## Verdict\n\n"
        f"- `needs-attention` if any finding is worth acting on; otherwise `approve`.\n"
    )


@app.command()
def init(
    name: Annotated[str, typer.Argument(help="Reviewer name (the dir + frontmatter name).")],
    with_harness: Annotated[
        bool,
        typer.Option(
            "--with-harness",
            help="Scaffold a harnesses: block (claude-code/opus) in the frontmatter.",
        ),
    ] = False,
) -> None:
    """Scaffold a repo reviewer (REVIEWER.md with its harnesses in the frontmatter)."""
    if name in RESERVED_REVIEWER_NAMES:
        typer.echo(f"aeview: '{name}' is reserved (it's a --reviewers keyword)", err=True)
        raise typer.Exit(EXIT_ERROR)
    if not _SAFE_REVIEWER_NAME.fullmatch(name):
        typer.echo(
            f"aeview: invalid reviewer name '{name}' (use letters, digits, '-' or '_')", err=True
        )
        raise typer.Exit(EXIT_ERROR)
    target = Path.cwd() / ".aeview" / "reviewers" / name
    # Claim the reviewer dir atomically (exclusive create) — the real guard against a concurrent
    # init and against adopting a crashed init's leftover dir (a marker-only check would race).
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        typer.echo(f"aeview: reviewer '{name}' already exists at {target}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc
    # REVIEWER.md is the visibility marker — discovery keys on it — written atomically so a
    # concurrent reader never sees a half-written marker. Harnesses live in its frontmatter now,
    # so there's a single file to create (--with-harness just adds the block).
    reviewer_md = target / "REVIEWER.md"
    atomic_write_text(reviewer_md, _starter_reviewer(name, with_harness))
    typer.echo(f"created reviewer '{name}':")
    typer.echo(f"  {reviewer_md}")
