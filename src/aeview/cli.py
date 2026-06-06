"""aeview command-line interface.

Increment 1 wires the vertical slice: `aeview run --scope working-tree` resolves the
default reviewer, bundles the working-tree diff, fans it across the configured harness,
merges, and writes report.json with a 0/1/2 exit code. `version` reports the build.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .bundle import build_bundle
from .config import Settings, ensure_seeded, load_settings
from .doctor import run_doctor
from .fanout import fan_out
from .merge import merge_reviews
from .prompt import compose_prompt
from .report import EXIT_ERROR, exit_code, render_human
from .resolve import (
    ResolveError,
    Reviewer,
    build_roster,
    discover_reviewers,
    resolve_reviewer,
)
from .runstore import RunStore, new_run_id, now_iso
from .schema import DedupPlan, Invocation, Report, RosterEntry, RunManifest
from .scope import ScopeError, parse_scope
from .scope import resolve as resolve_scope

app = typer.Typer(
    name="aeview",
    help="Fan code reviewers across agent harnesses and merge one verdict.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _main() -> None:
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
    output: Annotated[
        Path | None, typer.Option("--output", help="Also write report.json to this path.")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Print report.json instead of the human summary.")
    ] = False,
) -> None:
    """Run reviewers over a scope and emit a merged report."""
    cwd = Path.cwd()
    try:
        names = _split_reviewers(reviewers)
        stype, value = parse_scope(scope)
        patch_text = _read_patch(value) if stype == "patch" else None
        report = asyncio.run(
            _orchestrate(names, stype, value, cwd, include_dirty, allow_conflicts, patch_text)
        )
    except (ScopeError, ResolveError) as exc:
        typer.echo(f"aeview: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc

    rendered = json.dumps(report.model_dump(), indent=2) if json_out else render_human(report)
    typer.echo(rendered)
    if output is not None:
        output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    raise typer.Exit(exit_code(report))


def _split_reviewers(values: list[str] | None) -> list[str]:
    """Flatten --reviewers: comma-separated (a,b) and/or repeated (--reviewers a --reviewers b).

    Omitting --reviewers uses the default reviewer; passing it *blank* (e.g. `--reviewers ""`,
    usually an empty shell variable) is a mistake and errors rather than silently defaulting.
    """
    if values is None:
        return ["default"]
    names = [n.strip() for item in values for n in item.split(",") if n.strip()]
    if not names:
        raise ResolveError("--reviewers was given but empty; omit it to use the default reviewer")
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


async def _orchestrate(
    names: list[str],
    stype: str,
    value: str | None,
    cwd: Path,
    include_dirty: bool,
    allow_conflicts: bool,
    patch_text: str | None,
) -> Report:
    settings = load_settings()
    if "all" in names:
        discovered = discover_reviewers(cwd)
        if not discovered:
            raise ResolveError("no reviewers found via the walk-up from this directory")
        # `all` is a bulk request: one mis-configured reviewer shouldn't abort the rest.
        resolved_reviewers = _resolve_all_lenient(discovered, cwd, settings)
        if not resolved_reviewers:
            raise ResolveError("every discovered reviewer had invalid config")
        names = [r.name for r in resolved_reviewers]
    else:
        # Explicitly named reviewers fail fast — you asked for these specific ones.
        names = list(dict.fromkeys(names))  # de-dupe, preserve order
        resolved_reviewers = [resolve_reviewer(name, cwd, settings) for name in names]
    roster = build_roster(resolved_reviewers)
    if not roster:
        raise ResolveError("no harnesses resolved (check harness.json / fallbackReviewerHarnesses)")

    resolved = resolve_scope(stype, value, cwd, include_dirty, allow_conflicts, patch_text)
    if resolved.is_empty:
        raise ScopeError(f"nothing to review for scope '{stype}'")
    bundle = build_bundle(resolved)

    store = RunStore.create(new_run_id())
    typer.echo(f"run {store.run_id}", err=True)

    manifest = RunManifest(
        run_id=store.run_id,
        created_at=now_iso(),
        started_at=now_iso(),
        overall="running",
        invocation=Invocation(reviewers=names, scope=bundle.scope),
        roster=roster,
        dedup=_dedup_plan(roster, settings),
    )
    store.write_manifest(manifest)
    full_diff_path = store.write_bundle(bundle)

    prompt_by_reviewer = {
        r.name: compose_prompt(r, bundle, full_diff_path) for r in resolved_reviewers
    }
    for reviewer_name, prompt in prompt_by_reviewer.items():
        store.write_prompt(reviewer_name, prompt)

    results = await fan_out(store, roster, prompt_by_reviewer, cwd)
    report = await merge_reviews(results, settings, store, cwd)
    store.write_report(report)

    manifest.overall = "failed" if report.coverage.contributed == 0 else "done"
    manifest.finished_at = now_iso()
    store.write_manifest(manifest)
    return report
