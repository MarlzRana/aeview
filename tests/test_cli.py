from __future__ import annotations

from aeview.cli import _resolve_all_lenient, _split_reviewers
from aeview.config import HarnessInstance, Settings
from test_resolve import make_reviewer


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
