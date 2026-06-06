from __future__ import annotations

from aeview.cli import _resolve_all_lenient, _split_reviewers
from aeview.config import HarnessInstance, Settings
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


def test_blank_value_falls_back_to_default():
    assert _split_reviewers([""]) == ["default"]
    assert _split_reviewers([" , "]) == ["default"]


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


def test_resolve_all_lenient_skips_reserved_name_in_sweep(tmp_path, capsys):
    make_reviewer(tmp_path, "good", harnesses=[{"harness": "claude-code", "model": "m"}])
    make_reviewer(tmp_path, "all", harnesses=[{"harness": "claude-code", "model": "m"}])
    # A reviewer dir literally named `all` is reserved -> skipped (loudly), not run.
    resolved = _resolve_all_lenient(["all", "good"], tmp_path, _settings())
    assert [r.name for r in resolved] == ["good"]
    assert "skipping reviewer 'all'" in capsys.readouterr().err
