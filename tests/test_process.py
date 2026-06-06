from __future__ import annotations

from pathlib import Path

from aeview.process import run_async, run_sync

_BAD_CWD = Path("/no/such/dir/aeview-xyz")


def test_run_sync_missing_binary(tmp_path):
    res = run_sync(["definitely-not-a-real-binary-xyz"], cwd=tmp_path)
    assert res.returncode == 127
    assert "command not found" in res.stderr


def test_run_sync_missing_cwd():
    # A real binary, but a bad cwd -> the cwd is named as the cause, not the binary.
    res = run_sync(["git", "status"], cwd=_BAD_CWD)
    assert res.returncode == 127
    assert "working directory not found" in res.stderr
    assert "command not found" not in res.stderr


async def test_run_async_missing_binary(tmp_path):
    res = await run_async(["definitely-not-a-real-binary-xyz"], cwd=tmp_path)
    assert res.returncode == 127
    assert "command not found" in res.stderr


async def test_run_async_missing_cwd():
    res = await run_async(["git", "status"], cwd=_BAD_CWD)
    assert res.returncode == 127
    assert "working directory not found" in res.stderr
    assert "command not found" not in res.stderr
