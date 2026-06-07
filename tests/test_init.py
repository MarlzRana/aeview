from __future__ import annotations

import json

from typer.testing import CliRunner

from aeview.cli import app

runner = CliRunner()


def _reviewer_dir(repo, name):
    return repo / ".aeview" / "reviewers" / name


def test_init_creates_reviewer_without_harness(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["init", "myrev"])
    assert res.exit_code == 0
    md = _reviewer_dir(tmp_path, "myrev") / "REVIEWER.md"
    assert md.exists()
    assert "name: myrev" in md.read_text()
    assert not (md.parent / "harness.json").exists()  # optional → omitted by default


def test_init_with_harness_seeds_claude_opus(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["init", "myrev", "--with-harness"])
    assert res.exit_code == 0
    hj = _reviewer_dir(tmp_path, "myrev") / "harness.json"
    instance = json.loads(hj.read_text())["harnesses"][0]
    assert instance == {"harness": "claude-code", "model": "claude-opus-4-8"}


def test_init_scaffold_resolves(aeview_home, tmp_path, monkeypatch):
    # A freshly-scaffolded reviewer round-trips through resolution (valid frontmatter + harness).
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "myrev", "--with-harness"])
    res = runner.invoke(app, ["reviewers", "myrev"])
    assert res.exit_code == 0
    assert "claude-code-claude-opus-4-8" in res.output


def test_init_refuses_reserved_name(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["init", "all"])
    assert res.exit_code == 2
    assert "reserved" in res.output


def test_init_refuses_unsafe_name(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["init", "../escape"])
    assert res.exit_code == 2
    assert not (tmp_path.parent / "escape").exists()  # never wrote outside .aeview/reviewers


def test_init_refuses_existing(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "myrev"])
    res = runner.invoke(app, ["init", "myrev"])
    assert res.exit_code == 2
    assert "already exists" in res.output
