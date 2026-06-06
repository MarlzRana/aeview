from __future__ import annotations

from aeview.cli import _split_reviewers


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
