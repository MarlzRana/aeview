"""aeview command-line interface.

Increment 1 wires the vertical slice: `aeview run --scope working-tree` resolves the
default reviewer, bundles the working-tree diff, fans it across the configured harness,
merges, and writes report.json with a 0/1/2 exit code. `version` reports the build.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .bundle import build_bundle
from .config import ensure_seeded, load_settings
from .fanout import fan_out
from .merge import merge_reviews
from .prompt import compose_prompt
from .report import EXIT_ERROR, exit_code, render_human
from .resolve import ResolveError, build_roster, resolve_reviewer
from .runstore import RunStore, new_run_id, now_iso
from .schema import Invocation, Report, RunManifest
from .scope import ScopeError

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


@app.command()
def run(
    scope: Annotated[
        str, typer.Option("--scope", help="What to review (I1 supports: working-tree).")
    ] = "working-tree",
    reviewers: Annotated[
        list[str] | None,
        typer.Option("--reviewers", help="Reviewer names (I1 supports: default)."),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="Also write report.json to this path.")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Print report.json instead of the human summary.")
    ] = False,
) -> None:
    """Run reviewers over a scope and emit a merged report."""
    names = reviewers or ["default"]
    cwd = Path.cwd()
    try:
        report = asyncio.run(_orchestrate(names, scope, cwd))
    except (ScopeError, ResolveError) as exc:
        typer.echo(f"aeview: {exc}", err=True)
        raise typer.Exit(EXIT_ERROR) from exc

    rendered = json.dumps(report.model_dump(), indent=2) if json_out else render_human(report)
    typer.echo(rendered)
    if output is not None:
        output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    raise typer.Exit(exit_code(report))


async def _orchestrate(names: list[str], scope: str, cwd: Path) -> Report:
    settings = load_settings()
    resolved = [resolve_reviewer(name, settings) for name in names]
    roster = build_roster(resolved)
    if not roster:
        raise ResolveError("no harnesses configured (settings.json defaultHarnesses is empty)")

    bundle = build_bundle(scope, cwd)
    if bundle.is_empty:
        raise ScopeError(f"nothing to review for scope '{scope}'")

    store = RunStore.create(new_run_id())
    typer.echo(f"run {store.run_id}", err=True)

    manifest = RunManifest(
        run_id=store.run_id,
        created_at=now_iso(),
        started_at=now_iso(),
        overall="running",
        invocation=Invocation(reviewers=names, scope=bundle.scope),
        roster=roster,
    )
    store.write_manifest(manifest)
    store.write_bundle(bundle)

    prompt_by_reviewer = {r.name: compose_prompt(r, bundle) for r in resolved}
    for reviewer_name, prompt in prompt_by_reviewer.items():
        store.write_prompt(reviewer_name, prompt)

    results = await fan_out(store, roster, prompt_by_reviewer, cwd)
    report = merge_reviews(results)
    store.write_report(report)

    manifest.overall = "failed" if report.coverage.contributed == 0 else "done"
    manifest.finished_at = now_iso()
    store.write_manifest(manifest)
    return report
