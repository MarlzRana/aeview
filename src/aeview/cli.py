"""aeview command-line interface.

Increment 1 wires the vertical slice: `aeview run --scope working-tree` resolves the
default reviewer, bundles the working-tree diff, fans it across the configured harness,
merges, and writes report.json with a 0/1/2 exit code. `version` reports the build.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .bundle import Bundle, build_bundle
from .config import Settings, ensure_seeded, load_settings
from .doctor import run_doctor
from .fanout import fan_out
from .merge import merge_reviews
from .prompt import compose_prompt
from .report import EXIT_APPROVE, EXIT_ERROR, exit_code, render_human
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
    latest_run_id,
    list_manifests,
    new_run_id,
    now_iso,
    prune_runs,
)
from .schema import DedupPlan, Invocation, Report, RosterEntry, RunManifest, ScopeSpec
from .scope import ScopeError, parse_scope
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

    prune_runs(settings.retention)  # keep ~/.aeview/runs bounded — only ever on a real `run`
    report = asyncio.run(_execute(plan, settings, cwd))

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


@dataclass(slots=True)
class _Plan:
    """The resolved, side-effect-free run plan shared by a real run and `--dry-run`."""

    names: list[str]
    reviewers: list[Reviewer]
    roster: list[RosterEntry]
    bundle: Bundle


def _plan_run(
    names: list[str],
    stype: str,
    value: str | None,
    cwd: Path,
    include_dirty: bool,
    allow_conflicts: bool,
    patch_text: str | None,
    settings: Settings,
) -> _Plan:
    """Resolve reviewers + scope and build the bundle — the sync front half shared by `run` and
    `--dry-run`. Raises ScopeError/ResolveError; makes no model calls and writes no run dir."""
    if "all" in names:
        discovered = discover_reviewers(cwd)
        if not discovered:
            raise ResolveError("no reviewers found via the walk-up from this directory")
        # `all` is a bulk request: one mis-configured reviewer shouldn't abort the rest.
        reviewers = _resolve_all_lenient(discovered, cwd, settings)
        if not reviewers:
            raise ResolveError("every discovered reviewer had invalid config")
        names = [r.name for r in reviewers]
    else:
        # Explicitly named reviewers fail fast — you asked for these specific ones.
        names = list(dict.fromkeys(names))  # de-dupe, preserve order
        reviewers = [resolve_reviewer(name, cwd, settings) for name in names]
    roster = build_roster(reviewers)
    if not roster:
        raise ResolveError("no harnesses resolved (check harness.json / fallbackReviewerHarnesses)")

    resolved = resolve_scope(stype, value, cwd, include_dirty, allow_conflicts, patch_text)
    if resolved.is_empty:
        raise ScopeError(f"nothing to review for scope '{stype}'")
    return _Plan(names=names, reviewers=reviewers, roster=roster, bundle=build_bundle(resolved))


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
    )
    store.write_manifest(manifest)
    full_diff_path = store.write_bundle(plan.bundle)

    prompt_by_reviewer = {
        r.name: compose_prompt(r, plan.bundle, full_diff_path) for r in plan.reviewers
    }
    for reviewer_name, prompt in prompt_by_reviewer.items():
        store.write_prompt(reviewer_name, prompt)

    results = await fan_out(store, plan.roster, prompt_by_reviewer, cwd)
    report = await merge_reviews(results, settings, store, cwd)
    store.write_report(report)

    manifest.overall = "failed" if report.coverage.contributed == 0 else "done"
    manifest.finished_at = now_iso()
    store.write_manifest(manifest)
    return report


def _render_dry_run(plan: _Plan, settings: Settings) -> str:
    bundle = plan.bundle
    mode = "inline" if bundle.is_inline else "self-collect"
    lines = [
        "dry run — no model calls, nothing persisted",
        f"scope: {_scope_label(bundle.scope)}",
        f"bundle: {mode}, {bundle.diff_bytes} bytes",
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


def _load_manifest_or_exit(run_id: str | None) -> RunManifest:
    """Resolve a run-id (or the latest run) to its manifest, or exit 2 with a clear message."""
    rid = run_id or latest_run_id()
    if rid is None:
        typer.echo("aeview: no runs found", err=True)
        raise typer.Exit(EXIT_ERROR)
    try:
        return RunStore(rid).read_manifest()
    except (OSError, ValueError) as exc:
        typer.echo(f"aeview: run '{rid}' not found", err=True)
        raise typer.Exit(EXIT_ERROR) from exc


@app.command()
def status(
    run_id: Annotated[
        str | None, typer.Argument(help="Run id; defaults to the latest run.")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")] = False,
) -> None:
    """Show a run's progress: per-review state + coverage (defaults to the latest run)."""
    manifest = _load_manifest_or_exit(run_id)
    reviews = {r.id: r for r in RunStore(manifest.run_id).read_reviews()}
    rows = [
        {"id": e.id, "status": reviews[e.id].status if e.id in reviews else "pending"}
        for e in manifest.roster
    ]
    counts = Counter(r["status"] for r in rows)
    if json_out:
        typer.echo(
            json.dumps(
                {
                    "run_id": manifest.run_id,
                    "overall": manifest.overall,
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
    typer.echo(f"run {manifest.run_id}")
    typer.echo(f"state: {manifest.overall}")
    typer.echo(f"scope: {_scope_label(manifest.invocation.scope)}")
    parts = [f"{counts[s]} {s}" for s in _REVIEW_STATUS_ORDER if counts.get(s)]
    typer.echo(f"reviews: {', '.join(parts) or '0'} (of {len(rows)})")
    for r in rows:
        typer.echo(f"  [{r['status']}] {r['id']}")


@app.command()
def result(
    run_id: Annotated[
        str | None, typer.Argument(help="Run id; defaults to the latest run.")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")] = False,
) -> None:
    """Print the merged report for a finished run; exits with that run's 0/1/2 verdict code."""
    manifest = _load_manifest_or_exit(run_id)
    try:
        report = RunStore(manifest.run_id).read_report()
    except (OSError, ValueError) as exc:
        typer.echo(
            f"aeview: run '{manifest.run_id}' has no report yet (state: {manifest.overall}); "
            f"see `aeview status {manifest.run_id}`",
            err=True,
        )
        raise typer.Exit(EXIT_ERROR) from exc
    rendered = json.dumps(report.model_dump(), indent=2) if json_out else render_human(report)
    typer.echo(rendered)
    raise typer.Exit(exit_code(report))


def _run_row(manifest: RunManifest) -> dict:
    """One `list` row: verdict + coverage come from report.json when the run has one; a
    still-running or report-less run shows its run-state instead."""
    verdict: str = manifest.overall
    coverage: dict | None = None
    if manifest.overall != "running":
        try:
            report = RunStore(manifest.run_id).read_report()
        except (OSError, ValueError):
            report = None
        if report is not None:
            verdict = "error" if report.coverage.contributed == 0 else report.verdict
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
    so the source reads as repo-vs-global at a glance."""
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text


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

_SAFE_REVIEWER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_STARTER_HARNESS = (
    '{\n  "harnesses": [\n    { "harness": "claude-code", "model": "claude-opus-4-8" }\n  ]\n}\n'
)


def _starter_reviewer(name: str) -> str:
    return (
        f"---\n"
        f"name: {name}\n"
        f"description: TODO one-line summary of what this reviewer checks.\n"
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
        typer.Option("--with-harness", help="Also scaffold a harness.json (claude-code/opus)."),
    ] = False,
) -> None:
    """Scaffold a repo reviewer at .aeview/reviewers/<name>/ (REVIEWER.md, optional harness)."""
    if name in RESERVED_REVIEWER_NAMES:
        typer.echo(f"aeview: '{name}' is reserved (it's a --reviewers keyword)", err=True)
        raise typer.Exit(EXIT_ERROR)
    if not _SAFE_REVIEWER_NAME.match(name):
        typer.echo(
            f"aeview: invalid reviewer name '{name}' (use letters, digits, '-' or '_')", err=True
        )
        raise typer.Exit(EXIT_ERROR)
    target = Path.cwd() / ".aeview" / "reviewers" / name
    reviewer_md = target / "REVIEWER.md"
    if reviewer_md.exists():
        typer.echo(f"aeview: reviewer '{name}' already exists at {target}", err=True)
        raise typer.Exit(EXIT_ERROR)
    target.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    if with_harness:
        harness_json = target / "harness.json"
        harness_json.write_text(_STARTER_HARNESS, encoding="utf-8")
        created.append(harness_json)
    # REVIEWER.md is the visibility marker — discovery and the existing-reviewer guard both key
    # on it — so write it last. A crash mid-init then leaves a dir that's invisible to discovery
    # and safe to re-init, never a reviewer published without its intended harness.json.
    reviewer_md.write_text(_starter_reviewer(name), encoding="utf-8")
    created.append(reviewer_md)
    typer.echo(f"created reviewer '{name}':")
    for path in created:
        typer.echo(f"  {path}")
