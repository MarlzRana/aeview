from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aeview.cli import _resolve_all_lenient, _split_reviewers, app
from aeview.config import HarnessInstance, Settings
from aeview.resolve import ResolveError
from conftest import make_reviewer


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
