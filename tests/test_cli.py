from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aeview import __version__
from aeview.bundle import Bundle
from aeview.cli import (
    _dedup_plan,
    _display_path,
    _Plan,
    _render_dry_run,
    _resolve_all_lenient,
    _split_reviewers,
    app,
)
from aeview.config import HarnessInstance, Settings, runs_dir
from aeview.resolve import ResolveError
from aeview.runstore import RunStore
from aeview.schema import Invocation, RosterEntry, RunManifest, ScopeSpec
from conftest import make_reviewer


def _roster(n: int) -> list[RosterEntry]:
    return [
        RosterEntry(id=f"r{i}__claude-code-opus", reviewer=f"r{i}", harness="claude-code",
                    model="opus")
        for i in range(n)
    ]


def _settings_with_dedup() -> Settings:
    return Settings(
        deduplication_harness=HarnessInstance(harness="claude-code", model="opus", thinking="high")
    )


def test_dedup_plan_pinned_when_roster_gt_1():
    plan = _dedup_plan(_roster(2), _settings_with_dedup())
    assert plan is not None
    assert plan.id == "claude-code-opus-high"  # descriptor includes thinking
    assert plan.harness == "claude-code" and plan.model == "opus" and plan.thinking == "high"


def test_dedup_plan_none_for_single_review_roster():
    assert _dedup_plan(_roster(1), _settings_with_dedup()) is None


def test_dedup_plan_none_when_unconfigured():
    assert _dedup_plan(_roster(3), Settings(deduplication_harness=None)) is None


def test_default_when_none():
    assert _split_reviewers(None) == ["default"]


def test_comma_separated_single_flag():
    assert _split_reviewers(["default,concurrency,tests"]) == ["default", "concurrency", "tests"]


def test_repeated_flag():
    assert _split_reviewers(["default", "concurrency"]) == ["default", "concurrency"]


def test_mixed_and_whitespace():
    assert _split_reviewers(["a, b", "c"]) == ["a", "b", "c"]


def test_all_passthrough():
    assert _split_reviewers(["all"]) == ["all"]


def test_blank_value_errors():
    # --reviewers given but empty (e.g. an empty shell var) is a mistake, not a default.
    with pytest.raises(ResolveError, match="empty"):
        _split_reviewers([""])
    with pytest.raises(ResolveError, match="empty"):
        _split_reviewers([" , "])


def test_run_blank_reviewers_exits_error(aeview_home, tmp_path, monkeypatch):
    # End-to-end: the blank-reviewers error surfaces through run() as exit 2 with guidance.
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run", "--reviewers", "", "--scope", "working-tree"])
    assert result.exit_code == 2
    assert "empty" in result.output


def test_doctor_command_exit_codes(aeview_home, monkeypatch):
    from aeview import cli
    from aeview.doctor import Check, DoctorReport

    ok = DoctorReport([Check("harness:claude-code", "ok", "present")])
    monkeypatch.setattr(cli, "run_doctor", lambda cwd, settings: ok)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "harness:claude-code" in result.output

    bad = DoctorReport([Check("harness:codex", "fail", "codex not found on PATH")])
    monkeypatch.setattr(cli, "run_doctor", lambda cwd, settings: bad)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1


def _settings():
    return Settings(
        fallback_reviewer_harnesses=[HarnessInstance(harness="claude-code", model="m")]
    )


def test_resolve_all_lenient_skips_bad_reviewer(tmp_path, capsys):
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "m"}])
    bad = make_reviewer(tmp_path, "bad", harnesses=[{"harness": "claude-code", "model": "m"}])
    (bad / "harness.json").write_text("{broken json")
    resolved = _resolve_all_lenient(["good", "bad"], tmp_path, _settings())
    assert [r.name for r in resolved] == ["good"]  # bad one skipped
    assert "skipping reviewer 'bad'" in capsys.readouterr().err


def test_resolve_all_lenient_all_bad_returns_empty(tmp_path, capsys):
    bad = make_reviewer(tmp_path, "bad", harnesses=[{"harness": "claude-code", "model": "m"}])
    (bad / "harness.json").write_text("{broken json")
    # The only discovered reviewer is broken -> empty list (the run's hard-error guard).
    assert _resolve_all_lenient(["bad"], tmp_path, _settings()) == []
    assert "skipping reviewer 'bad'" in capsys.readouterr().err


def test_resolve_all_lenient_skips_bad_yaml_frontmatter(tmp_path, capsys):
    # End-to-end leniency: a YAMLError in frontmatter (normalized to ResolveError) is skipped.
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "m"}])
    bad = tmp_path / ".aeview" / "reviewers" / "bad"
    bad.mkdir(parents=True)
    (bad / "REVIEWER.md").write_text("---\nname: [unclosed\n---\nbody\n")  # invalid YAML
    resolved = _resolve_all_lenient(["bad", "good"], tmp_path, _settings())
    assert [r.name for r in resolved] == ["good"]
    assert "skipping reviewer 'bad'" in capsys.readouterr().err


def test_resolve_all_lenient_skips_reserved_name_in_sweep(tmp_path, capsys):
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "m"}])
    make_reviewer(tmp_path, "all", harnesses=[{"harness": "claude-code", "model": "m"}])
    # A reviewer dir literally named `all` is reserved -> skipped (loudly), not run.
    resolved = _resolve_all_lenient(["all", "good"], tmp_path, _settings())
    assert [r.name for r in resolved] == ["good"]
    assert "skipping reviewer 'all'" in capsys.readouterr().err


def test_version_flag(aeview_home):
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_dry_run_persists_nothing(aeview_home, git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    result = CliRunner().invoke(app, ["run", "--scope", "working-tree", "--dry-run"])
    assert result.exit_code == 0
    assert "dry run" in result.output
    assert "roster" in result.output
    assert not any(runs_dir().iterdir())  # zero model calls AND no run dir written


def _stale_run(run_id: str = "stale-run") -> None:
    RunStore.create(run_id).write_manifest(
        RunManifest(
            run_id=run_id,
            created_at="2000-01-01T00:00:00Z",
            overall="done",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=[],
        )
    )


def test_run_prunes_stale_terminal_runs(aeview_home, git_repo, stub_claude, monkeypatch):
    # A real `run` triggers retention prune. keepLast=0 (no floor) so the aged stale run is the
    # outside-floor-AND-too-old case the pruner deletes.
    import json

    monkeypatch.chdir(git_repo)
    aeview_home.mkdir(parents=True, exist_ok=True)
    settings = {"retention": {"keepLast": 0, "ttlDays": 14}}
    (aeview_home / "settings.json").write_text(json.dumps(settings))
    _stale_run()
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    CliRunner().invoke(app, ["run", "--scope", "working-tree"])
    assert "stale-run" not in {p.name for p in runs_dir().iterdir()}


def test_run_reconciles_and_prunes_crashed_running_run(aeview_home, git_repo, stub_claude,
                                                       monkeypatch):
    # A crashed run stuck 'running' (dead pid) is reconciled to 'interrupted' at the start of a
    # real run, which makes it a prune candidate (keepLast=0 + old) -> the stuck-running leak is
    # collected instead of lingering forever.
    import json

    monkeypatch.chdir(git_repo)
    aeview_home.mkdir(parents=True, exist_ok=True)
    settings = {"retention": {"keepLast": 0, "ttlDays": 14}}
    (aeview_home / "settings.json").write_text(json.dumps(settings))
    RunStore.create("crashed").write_manifest(
        RunManifest(
            run_id="crashed",
            created_at="2000-01-01T00:00:00Z",
            overall="running",
            invocation=Invocation(reviewers=["default"], scope=ScopeSpec(type="working-tree")),
            roster=[],
            pid=999_999,  # dead
        )
    )
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    CliRunner().invoke(app, ["run", "--scope", "working-tree"])
    assert "crashed" not in {p.name for p in runs_dir().iterdir()}


def test_dry_run_does_not_prune_existing_runs(aeview_home, git_repo, monkeypatch):
    # --dry-run persists nothing AND has no side effects: an old terminal run is NOT pruned.
    monkeypatch.chdir(git_repo)
    _stale_run()
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    result = CliRunner().invoke(app, ["run", "--scope", "working-tree", "--dry-run"])
    assert result.exit_code == 0
    assert (runs_dir() / "stale-run").exists()  # preview must not delete history


def test_run_writes_output_file(aeview_home, git_repo, stub_claude, tmp_path, monkeypatch):
    # Positive half of the --output contract (the dry-run test covers the negative half).
    import json

    monkeypatch.chdir(git_repo)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    out = tmp_path / "report.json"
    CliRunner().invoke(app, ["run", "--scope", "working-tree", "--output", str(out)])
    assert out.exists()
    assert "verdict" in json.loads(out.read_text())


def test_dry_run_does_not_write_output(aeview_home, git_repo, tmp_path, monkeypatch):
    # "persist nothing" includes --output: the preview exits before any report write.
    monkeypatch.chdir(git_repo)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    out = tmp_path / "should_not_exist.json"
    result = CliRunner().invoke(
        app, ["run", "--scope", "working-tree", "--dry-run", "--output", str(out)]
    )
    assert result.exit_code == 0
    assert not out.exists()


def _dry_plan(n_reviews: int = 1, *, mode: str = "inline", thinking: str | None = None) -> _Plan:
    roster = [
        RosterEntry(id=f"r__h{i}", reviewer="r", harness="claude-code", model=f"m{i}",
                    thinking=thinking)
        for i in range(n_reviews)
    ]
    bundle = Bundle(
        mode=mode, scope=ScopeSpec(type="branch", base="main"),
        diff="x", summary="s", diff_bytes=123,
    )
    return _Plan(names=["r"], reviewers=[], roster=roster, bundle=bundle)


def test_dry_run_render_single_review_skips_dedup():
    out = _render_dry_run(_dry_plan(1), Settings(deduplication_harness=None))
    assert "scope: branch (base main)" in out
    assert "bundle: inline, 123 bytes" in out
    assert "roster (1 review):" in out  # singular
    assert "dedup: skipped (single review)" in out


def test_dry_run_render_multi_with_dedup_harness():
    out = _render_dry_run(_dry_plan(2), _settings_with_dedup())
    assert "roster (2 reviews):" in out  # plural
    assert "dedup: claude-code opus" in out


def test_dry_run_render_multi_without_dedup_harness():
    out = _render_dry_run(_dry_plan(2), Settings(deduplication_harness=None))
    assert "dedup: not configured" in out


def test_dry_run_render_lists_roster_entries():
    # The per-entry roster preview is the point of --dry-run; pin one entry line + thinking suffix.
    out = _render_dry_run(_dry_plan(1, thinking="high"), Settings(deduplication_harness=None))
    assert "  - r__h0  (claude-code m0 thinking=high)" in out


def test_dry_run_render_self_collect_mode_label():
    out = _render_dry_run(_dry_plan(1, mode="self-collect"), Settings(deduplication_harness=None))
    assert "bundle: self-collect, 123 bytes" in out


def test_failed_planning_does_not_prune(aeview_home, tmp_path, monkeypatch):
    # prune runs only after planning succeeds: an erroring command must not delete history.
    monkeypatch.chdir(tmp_path)
    _stale_run()
    result = CliRunner().invoke(app, ["run", "--reviewers", "nope", "--scope", "working-tree"])
    assert result.exit_code == 2  # unknown reviewer -> planning fails before prune
    assert (runs_dir() / "stale-run").exists()


def test_display_path_collapses_home_with_boundary_guard(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert _display_path(home / ".aeview" / "x") == "~/.aeview/x"
    assert _display_path(home) == "~"
    # a sibling that merely shares the home string as a prefix must NOT be collapsed
    assert _display_path(tmp_path / "homework") == str(tmp_path / "homework")
