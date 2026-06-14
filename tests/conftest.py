from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


def make_reviewer(
    base: Path,
    name: str,
    *,
    body: str = "BODY",
    fm_name: str | None = None,
    harnesses: list[dict[str, str]] | None = None,
    auto_activate_paths: list[str] | None = None,
) -> Path:
    """Create a reviewer at <base>/.aeview/reviewers/<name>/ with harnesses in its frontmatter.

    `harnesses` is a list of dicts (or [] for an explicit empty block); None omits the block so
    the reviewer falls back to settings.fallbackReviewerHarnesses. `auto_activate_paths` is a list
    of globs (or None to omit the key) written under the kebab-case `auto-activate-paths` alias.
    JSON is valid YAML, so each list is embedded as a flow sequence in the frontmatter."""
    d = base / ".aeview" / "reviewers" / name
    d.mkdir(parents=True, exist_ok=True)
    front = [f"name: {fm_name or name}", "description: d"]
    if harnesses is not None:
        front.append(f"harnesses: {json.dumps(harnesses)}")
    if auto_activate_paths is not None:
        front.append(f"auto-activate-paths: {json.dumps(auto_activate_paths)}")
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


_STUB_OK = {
    "verdict": "needs-attention",
    "summary": "One issue found.",
    "findings": [
        {
            "title": "Unhandled None input",
            "body": "add() will raise on None.",
            "severity": "high",
            "category": "bug",
            "confidence": 0.8,
            "location": {"file": "app.py", "line_start": 2, "line_end": 2},
            "recommendation": "Guard against None before adding.",
        }
    ],
    "next_steps": ["Add a None guard."],
}
_STUB_APPROVE = {
    "verdict": "approve",
    "summary": "Looks correct.",
    "findings": [],
    "next_steps": [],
}


def _stub_result(structured, *, is_error: bool = False, result: str = "") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="stub",
        total_cost_usd=0.0123,
        usage={"input_tokens": 100, "output_tokens": 50},
        result=result,
        structured_output=structured,
    )


@pytest.fixture
def stub_claude(monkeypatch):
    """Mock the claude SDK boundary (claude_code.query) with an offline async-generator stub. The
    claude adapter resolves the SDK's bundled binary (not a PATH stub), so Tier-1 tests intercept
    the SDK call itself. Returns a mode setter; modes mirror the old CLI stub (ok/approve/error/
    malformed). A dedup call (its schema carries duplicate_groups) returns an empty grouping."""
    from aeview.harness import claude_code

    state = {"mode": "ok"}

    async def fake_query(*, prompt, options, transport=None):
        if "duplicate_groups" in json.dumps(options.output_format):
            yield _stub_result({"duplicate_groups": []})  # dedup ok: findings pass through
            return
        yield AssistantMessage(content=[TextBlock(text="stub review")], model="claude-opus-4-8")
        mode = state["mode"]
        if mode == "error":
            yield _stub_result(None, is_error=True, result="stub harness error")  # non-transient
        elif mode == "approve":
            yield _stub_result(_STUB_APPROVE)
        elif mode == "malformed":
            yield _stub_result({"summary": "missing verdict"})  # run() schema-validation fails
        else:  # ok
            yield _stub_result(_STUB_OK)

    monkeypatch.setattr(claude_code, "query", fake_query)

    def set_mode(mode: str) -> None:
        state["mode"] = mode

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
