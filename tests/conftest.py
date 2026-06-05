from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


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

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", "/dev/null")
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    git("add", "app.py")
    git("commit", "-q", "--no-verify", "-m", "init")
    return repo
