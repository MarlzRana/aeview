"""Async and sync subprocess helpers for git / gh / harness CLIs."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


def run_sync(args: list[str], cwd: Path | None = None) -> ProcResult:
    proc = subprocess.run(  # noqa: S603 - args are constructed internally, not shell
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return ProcResult(proc.returncode, proc.stdout, proc.stderr)


async def run_async(
    args: list[str],
    cwd: Path | None = None,
    log_path: Path | None = None,
) -> ProcResult:
    """Run a command, capturing stdout/stderr. Optionally tee raw output to a log file.

    Harness CLIs buffer their output and only surface it on exit, so there is no value
    in streaming line-by-line here; we capture fully, then persist the raw bytes.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if log_path is not None:
        log_path.write_text(stdout + ("\n--- stderr ---\n" + stderr if stderr else ""), "utf-8")
    return ProcResult(proc.returncode or 0, stdout, stderr)
