from __future__ import annotations

import json

from typer.testing import CliRunner

from aeview.cli import app
from conftest import make_reviewer

runner = CliRunner()

_HARNESS = [{"harness": "claude-code", "model": "opus"}]


def test_reviewers_lists_discovered_with_source_and_harnesses(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_reviewer(tmp_path, "sec", harnesses=_HARNESS)
    res = runner.invoke(app, ["reviewers"])
    assert res.exit_code == 0
    assert "sec" in res.output
    assert "default" in res.output  # seeded into ~/.aeview, found via the home rung
    assert "claude-code-opus" in res.output  # resolved harness instance id


def test_reviewers_flags_shadowed(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_reviewer(tmp_path, "dup", harnesses=_HARNESS)  # nearer (repo) rung wins
    make_reviewer(aeview_home.parent, "dup", harnesses=_HARNESS)  # ~/.aeview copy is shadowed
    rows = json.loads(runner.invoke(app, ["reviewers", "--json"]).output)
    dup = next(r for r in rows if r["name"] == "dup")
    # Assert the *direction*: the repo dir wins (shown absolute) and the home copy is shadowed.
    # A winner/shadowed inversion (home overriding repo) would still print "shadows:" but fail here.
    assert dup["source"].endswith("/.aeview/reviewers/dup")
    assert not dup["source"].startswith("~")
    assert dup["shadows"] == ["~/.aeview/reviewers/dup"]


def test_reviewers_invalid_config_shown_not_fatal(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bad = make_reviewer(tmp_path, "bad", harnesses=_HARNESS)
    (bad / "harness.json").write_text("{broken json")
    res = runner.invoke(app, ["reviewers"])
    assert res.exit_code == 0  # listing tolerates a broken reviewer
    assert "INVALID" in res.output


def test_reviewers_detail_shows_thinking(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_reviewer(
        tmp_path, "sec", harnesses=[{"harness": "claude-code", "model": "opus", "thinking": "high"}]
    )
    res = runner.invoke(app, ["reviewers", "sec"])
    assert res.exit_code == 0
    assert "reviewer: sec" in res.output
    assert "thinking=high" in res.output


def test_reviewers_detail_json(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_reviewer(tmp_path, "sec", harnesses=_HARNESS)
    res = runner.invoke(app, ["reviewers", "sec", "--json"])
    data = json.loads(res.output)
    assert data["name"] == "sec"
    assert data["harnesses"][0]["harness"] == "claude-code"


def test_reviewers_unknown_name_exits_error(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["reviewers", "does-not-exist"])
    assert res.exit_code == 2
