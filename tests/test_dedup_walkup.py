"""DEDUPLICATION.md walk-up discovery + the run-dir freeze that keeps resume byte-identical.

The dedup prompt is discovered by the same cwd->home, `.aeview/`-scoped, first-match-wins walk-up
as reviewers (`resolve_dedup_prompt`), frozen to `dedup/DEDUPLICATION.md` at run start, and read
back at merge time so a re-merge on resume never re-reads a since-edited source. Home's seeded
`~/.aeview/DEDUPLICATION.md` is the always-present terminal fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aeview import cli
from aeview import merge as merge_mod
from aeview.cli import app
from aeview.dedup import _compose
from aeview.resolve import DedupPromptSource, resolve_dedup_prompt
from aeview.runstore import RunStore, latest_run_id, new_run_id
from aeview.schema import (
    Coverage,
    Dedup,
    DedupPlan,
    DuplicateGroup,
    Invocation,
    PooledFinding,
    Report,
    RosterEntry,
    RunManifest,
    ScopeSpec,
    UsageBreakdown,
)
from conftest import make_reviewer


def _finding_dict(fid: str, title: str) -> dict:
    # Built as a dict (validated in) because the descriptive slots ride on the loose Finding carrier
    # as extra — model_validate is the pyright-clean way to construct one with those slots.
    return {
        "id": fid,
        "title": title,
        "body": "b",
        "severity": "high",
        "category": "bug",
        "confidence": 0.7,
        "location": {"file": "a.py", "line_start": 1, "line_end": 1},
        "recommendation": "fix",
    }


def _pool() -> list[PooledFinding]:
    return [PooledFinding.model_validate(_finding_dict("f1", "t"))]


def _report() -> Report:
    return Report(
        verdict="needs-attention",
        summary="s",
        findings=[],
        next_steps=[],
        coverage=Coverage(contributed=2, failed=0),
        dedup=Dedup(status="skipped"),
        usage=UsageBreakdown(),
    )


# --- resolve_dedup_prompt: the walk-up --------------------------------------------------------


def test_home_fallback_when_no_repo_prompt(aeview_home):
    # No `.aeview/DEDUPLICATION.md` between cwd and home -> the always-seeded home file wins.
    repo = aeview_home.parent / "repo"
    repo.mkdir()
    resolved = resolve_dedup_prompt(repo)
    assert isinstance(resolved, DedupPromptSource)
    assert resolved.source == aeview_home / "DEDUPLICATION.md"


def test_repo_prompt_wins_over_home(aeview_home):
    repo = aeview_home.parent / "repo"
    (repo / ".aeview").mkdir(parents=True)
    (repo / ".aeview" / "DEDUPLICATION.md").write_text("---\nname: d\n---\nREPO RULES\n")
    resolved = resolve_dedup_prompt(repo)
    assert resolved.source == repo / ".aeview" / "DEDUPLICATION.md"
    assert resolved.text == "REPO RULES\n"  # frontmatter stripped


def test_nearest_rung_wins_first_match(aeview_home):
    # Two rungs on the climb define a prompt; the nearest (deepest) one wins.
    mid = aeview_home.parent / "a"
    near = mid / "b"
    (mid / ".aeview").mkdir(parents=True)
    (mid / ".aeview" / "DEDUPLICATION.md").write_text("MID\n")
    (near / ".aeview").mkdir(parents=True)
    (near / ".aeview" / "DEDUPLICATION.md").write_text("NEAR\n")
    resolved = resolve_dedup_prompt(near)
    assert resolved.source == near / ".aeview" / "DEDUPLICATION.md"
    assert resolved.text == "NEAR\n"


def test_loose_prompt_not_in_aeview_is_ignored(aeview_home):
    # Only `<rung>/.aeview/DEDUPLICATION.md` counts; a bare DEDUPLICATION.md in cwd is not the
    # dedup prompt (mirrors reviewers living under `.aeview/`), so the walk-up falls to home.
    repo = aeview_home.parent / "repo"
    repo.mkdir()
    (repo / "DEDUPLICATION.md").write_text("LOOSE, IGNORED\n")
    resolved = resolve_dedup_prompt(repo)
    assert resolved.source == aeview_home / "DEDUPLICATION.md"
    assert "LOOSE, IGNORED" not in resolved.text


# --- freeze + resume wiring -------------------------------------------------------------------


def test_run_freezes_dedup_prompt_source(aeview_home, git_repo, monkeypatch):
    # A multi-review run resolves DEDUPLICATION.md by walk-up, freezes its text to
    # dedup/DEDUPLICATION.md, and pins the source path in run.json. fan_out is mocked: roster > 1
    # alone pins the dedup plan, so the freeze is asserted regardless of the (unrun) harness.
    make_reviewer(
        git_repo,
        "cs",
        harnesses=[
            {"harness": "claude-code", "model": "opus"},
            {"harness": "claude-code", "model": "sonnet"},
        ],
    )
    (git_repo / ".aeview" / "DEDUPLICATION.md").write_text("---\nname: d\n---\nREPO DEDUP RULES\n")

    async def fake_fan_out(
        s,
        roster,
        prompts,
        cwd,
        timeout=None,
        override_harness_binaries=None,
        schema_by_reviewer=None,
    ):
        return []

    monkeypatch.setattr(cli, "fan_out", fake_fan_out)
    monkeypatch.chdir(git_repo)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    CliRunner().invoke(app, ["run", "--reviewers", "cs", "--scope", "working-tree"])

    rid = latest_run_id()
    assert rid is not None
    store = RunStore(rid)
    assert store.read_dedup_prompt_source() == "REPO DEDUP RULES\n"  # frontmatter stripped, frozen
    manifest = store.read_manifest()
    assert manifest.dedup is not None
    assert manifest.dedup.prompt_source == (git_repo / ".aeview" / "DEDUPLICATION.md").resolve()


def test_single_review_run_does_not_freeze_dedup_prompt(aeview_home, git_repo, monkeypatch):
    # roster == 1 -> dedup can't run -> nothing is frozen and no source is pinned.
    make_reviewer(git_repo, "cs", harnesses=[{"harness": "claude-code", "model": "opus"}])

    async def fake_fan_out(
        s,
        roster,
        prompts,
        cwd,
        timeout=None,
        override_harness_binaries=None,
        schema_by_reviewer=None,
    ):
        return []

    monkeypatch.setattr(cli, "fan_out", fake_fan_out)
    monkeypatch.chdir(git_repo)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    CliRunner().invoke(app, ["run", "--reviewers", "cs", "--scope", "working-tree"])

    rid = latest_run_id()
    assert rid is not None
    store = RunStore(rid)
    assert store.read_manifest().dedup is None
    with pytest.raises((OSError, ValueError)):
        store.read_dedup_prompt_source()


def _dedup_manifest(store: RunStore, dedup: DedupPlan | None) -> RunManifest:
    return RunManifest(
        run_id=store.run_id,
        created_at="2026-08-01T00:00:00Z",
        overall="running",
        invocation=Invocation(reviewers=["r"], scope=ScopeSpec(type="working-tree")),
        roster=[
            RosterEntry(id="r__claude-code-m", reviewer="r", harness="claude-code", model="m"),
            RosterEntry(id="r__claude-code-n", reviewer="r", harness="claude-code", model="n"),
        ],
        dedup=dedup,
    )


async def test_re_merge_reads_frozen_prompt_not_live_source(aeview_home, monkeypatch):
    # The byte-identical invariant: a re-merge reads the frozen dedup/DEDUPLICATION.md, never a
    # since-edited source. A divergent home prompt is written to prove it is NOT what merge sees.
    store = RunStore.create(new_run_id())
    store.write_dedup_prompt_source("FROZEN DEDUP PROMPT")
    (aeview_home / "DEDUPLICATION.md").write_text("---\nname: d\n---\nLIVE DIVERGED PROMPT\n")
    src = Path("/some/.aeview/DEDUPLICATION.md")
    manifest = _dedup_manifest(
        store, DedupPlan(id="claude-code-x", harness="claude-code", model="x", prompt_source=src)
    )

    captured: dict = {}

    async def fake_merge(results, settings, s, cwd, dedup_prompt=None, dedup_prompt_source=None):
        captured["prompt"] = dedup_prompt
        captured["source"] = dedup_prompt_source
        return _report()

    monkeypatch.setattr(cli, "merge_reviews", fake_merge)
    await cli._run_reviews_and_merge(store, manifest, [], {}, {}, aeview_home.parent, None, {})
    assert captured["prompt"] == "FROZEN DEDUP PROMPT"  # frozen bytes, not the live home file
    assert captured["source"] == src


async def test_re_merge_tolerates_missing_frozen_prompt(aeview_home, monkeypatch):
    # An older run (or one that never pinned a dedup plan) has no frozen file -> None is threaded,
    # and dedup falls back to a live home read downstream.
    store = RunStore.create(new_run_id())
    manifest = _dedup_manifest(store, None)

    captured: dict = {}

    async def fake_merge(results, settings, s, cwd, dedup_prompt=None, dedup_prompt_source=None):
        captured["prompt"] = dedup_prompt
        captured["source"] = dedup_prompt_source
        return _report()

    monkeypatch.setattr(cli, "merge_reviews", fake_merge)
    await cli._run_reviews_and_merge(store, manifest, [], {}, {}, aeview_home.parent, None, {})
    assert captured["prompt"] is None
    assert captured["source"] is None


# --- _compose: frozen prompt vs the home fallback ---------------------------------------------


def test_compose_uses_frozen_prompt(aeview_home):
    out = _compose(_pool(), "FROZEN INSTRUCTIONS")
    assert out.startswith("FROZEN INSTRUCTIONS")
    assert "## Findings to deduplicate" in out  # the pool is still fenced in as untrusted data


def test_compose_falls_back_to_home_when_none(aeview_home):
    aeview_home.mkdir(parents=True, exist_ok=True)  # write our own before ensure_seeded fills it
    (aeview_home / "DEDUPLICATION.md").write_text("---\nname: d\n---\nHOME INSTRUCTIONS\n")
    out = _compose(_pool(), None)
    assert out.startswith("HOME INSTRUCTIONS")


# --- the source is threaded into run_dedup + persisted ----------------------------------------


async def test_merge_threads_frozen_prompt_and_source_to_run_dedup(aeview_home, monkeypatch):
    from aeview.config import HarnessInstance, Settings
    from aeview.dedup import DedupOutcome
    from aeview.schema import Finding, ReviewResult, Usage

    def _review(rid: str, fid: str, title: str) -> ReviewResult:
        return ReviewResult(
            id=rid,
            reviewer="r",
            harness="claude-code",
            model="m",
            status="done",
            verdict="needs-attention",
            summary="s",
            findings=[Finding.model_validate(_finding_dict(fid, title))],
            next_steps=[],
        )

    captured: dict = {}

    async def fake_run_dedup(
        pool,
        instance,
        store,
        cwd,
        timeout,
        binary_override=None,
        dedup_prompt=None,
        prompt_source=None,
    ):
        captured["dedup_prompt"] = dedup_prompt
        captured["prompt_source"] = prompt_source
        return DedupOutcome(
            "ok", [DuplicateGroup(survivor="f1", duplicates=["f2"])], Usage(), "h", prompt_source
        )

    monkeypatch.setattr(merge_mod, "run_dedup", fake_run_dedup)
    src = Path("/repo/.aeview/DEDUPLICATION.md")
    settings = Settings(deduplication_harness=HarnessInstance(harness="claude-code", model="m"))
    report = await merge_mod.merge_reviews(
        [_review("r__claude-code-a", "fa", "x"), _review("r__codex-b", "fb", "y")],
        settings,
        RunStore.create(new_run_id()),
        aeview_home.parent,
        dedup_prompt="FROZEN",
        dedup_prompt_source=src,
    )
    assert captured["dedup_prompt"] == "FROZEN"
    assert captured["prompt_source"] == src
    assert report.dedup.prompt_source == src  # provenance surfaced on the report too
