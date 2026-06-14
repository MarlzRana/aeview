from __future__ import annotations

from pathlib import Path

from aeview.process import run_async, run_sync

_BAD_CWD = Path("/no/such/dir/aeview-xyz")


def test_run_sync_missing_binary(tmp_path):
    res = run_sync(["definitely-not-a-real-binary-xyz"], cwd=tmp_path)
    assert res.returncode == 127
    assert "command not found" in res.stderr


def test_run_sync_non_executable_binary(tmp_path):
    # A path that exists but isn't executable (e.g. a bad harnessBinaries override) is a failed
    # spawn (PermissionError), not an uncaught crash — doctor probes binaries by path.
    f = tmp_path / "not-exec"
    f.write_text("data")  # mode 644: no execute bit
    res = run_sync([str(f)], cwd=tmp_path)
    assert res.returncode == 127


async def test_run_async_non_executable_binary(tmp_path):
    # Async twin: the review fan-out path also degrades a non-executable binary to a failed result.
    f = tmp_path / "not-exec"
    f.write_text("data")
    res = await run_async([str(f)], cwd=tmp_path)
    assert res.returncode == 127


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


def test_run_sync_timeout():
    res = run_sync(["sleep", "5"], timeout=0.1)
    assert res.returncode == 124
    assert "timed out" in res.stderr


async def test_run_async_timeout_kills_and_logs(tmp_path):
    # The dedup fail-loud path rests on this: a wedged harness is killed and reported as 124.
    log = tmp_path / "out.log"
    res = await run_async(["sleep", "5"], cwd=tmp_path, log_path=log, timeout=0.1)
    assert res.returncode == 124
    assert "timed out" in res.stderr
    assert "timed out" in log.read_text()  # the timeout is persisted to the log


async def test_run_async_no_timeout_completes(tmp_path):
    res = await run_async(["printf", "hi"], cwd=tmp_path)
    assert res.returncode == 0
    assert res.stdout == "hi"
