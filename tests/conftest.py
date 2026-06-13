from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def make_reviewer(base: Path, name: str, *, body="BODY", fm_name=None, harnesses=None) -> Path:
    """Create a reviewer at <base>/.aeview/reviewers/<name>/ with harnesses in its frontmatter.

    `harnesses` is a list of dicts (or [] for an explicit empty block); None omits the block so
    the reviewer falls back to settings.fallbackReviewerHarnesses. JSON is valid YAML, so the
    list is embedded as a flow sequence in the frontmatter."""
    d = base / ".aeview" / "reviewers" / name
    d.mkdir(parents=True, exist_ok=True)
    front = [f"name: {fm_name or name}", "description: d"]
    if harnesses is not None:
        front.append(f"harnesses: {json.dumps(harnesses)}")
    fm = "\n".join(front)
    (d / "REVIEWER.md").write_text(f"---\n{fm}\n---\n{body}\n")
    return d


@pytest.fixture
def aeview_home(tmp_path, monkeypatch):
    """Point ~/.aeview at a temp dir so seeding and runs never touch the real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home / ".aeview"


@pytest.fixture
def stub_claude(monkeypatch):
    """Put the offline `claude` stub first on PATH; return a mode setter."""
    stub_dir = Path(__file__).parent / "stubs"
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    def set_mode(mode: str) -> None:
        monkeypatch.setenv("AEVIEW_STUB_MODE", mode)

    set_mode("ok")
    return set_mode


@pytest.fixture
def git_repo(tmp_path):
    """A throwaway git repo with one commit, returned as a Path."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-b", "main", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", "/dev/null")
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    git("add", "app.py")
    git("commit", "-q", "--no-verify", "-m", "init")
    return repo


def git(repo, *args: str) -> str:
    """Run a git command in `repo`, returning stdout (raises on failure)."""
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def commit(repo, filename: str, content: str, message: str) -> str:
    """Write a file, stage it, commit it; return the new commit sha."""
    (repo / filename).write_text(content)
    git(repo, "add", filename)
    git(repo, "commit", "-q", "--no-verify", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def stub_gh(monkeypatch):
    """Put the offline `gh` stub first on PATH (works with stub_claude's PATH edit)."""
    stub_dir = Path(__file__).parent / "stubs"
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("AEVIEW_GH_BASE", "main")
