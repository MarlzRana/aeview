from __future__ import annotations

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
    text = md.read_text()
    assert 'name: "myrev"' in text  # quoted so YAML-1.1 keywords stay strings
    assert "harnesses:" not in text  # optional → omitted by default (uses fallback)


def test_init_with_harness_writes_frontmatter_harnesses(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["init", "myrev", "--with-harness"])
    assert res.exit_code == 0
    text = (_reviewer_dir(tmp_path, "myrev") / "REVIEWER.md").read_text()
    assert "harnesses:" in text
    assert "{ harness: claude-code, model: claude-opus-4-8 }" in text


def test_init_scaffold_resolves(aeview_home, tmp_path, monkeypatch):
    # A freshly-scaffolded reviewer round-trips through resolution (valid frontmatter + harness).
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "myrev", "--with-harness"])
    res = runner.invoke(app, ["reviewers", "myrev"])
    assert res.exit_code == 0
    assert "claude-code-claude-opus-4-8" in res.output


def test_init_yaml_keyword_name_resolves(aeview_home, tmp_path, monkeypatch):
    # `yes` is a YAML-1.1 boolean; the quoted frontmatter must keep it the string name "yes" so
    # the scaffolded reviewer resolves (dir name == frontmatter name) instead of becoming True.
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "yes", "--with-harness"]).exit_code == 0
    res = runner.invoke(app, ["reviewers", "yes"])
    assert res.exit_code == 0
    assert "reviewer: yes" in res.output


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


def test_init_refuses_embedded_slash_name(aeview_home, tmp_path, monkeypatch):
    # Valid prefix + a slash: pins the fullmatch invariant (a `.match` regression would accept
    # the "a" prefix and let mkdir/REVIEWER.md escape .aeview/reviewers).
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["init", "a/../../escape"])
    assert res.exit_code == 2
    assert not (tmp_path.parent / "escape").exists()


def test_init_refuses_existing(aeview_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "myrev"])
    res = runner.invoke(app, ["init", "myrev"])
    assert res.exit_code == 2
    assert "already exists" in res.output


def test_init_refuses_partial_leftover_dir(aeview_home, tmp_path, monkeypatch):
    # A crashed init can leave a reviewer dir without its REVIEWER.md marker. The exclusive-mkdir
    # claim must refuse the existing dir, never publish a marker into stale leftovers.
    monkeypatch.chdir(tmp_path)
    d = _reviewer_dir(tmp_path, "foo")
    d.mkdir(parents=True)
    (d / "leftover.tmp").write_text("partial")
    res = runner.invoke(app, ["init", "foo"])
    assert res.exit_code == 2
    assert "already exists" in res.output
    assert not (d / "REVIEWER.md").exists()  # the stale dir was not adopted
