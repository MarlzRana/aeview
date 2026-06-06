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


_CMD_NOT_FOUND = 127  # conventional shell exit code for a missing executable


def run_sync(args: list[str], cwd: Path | None = None) -> ProcResult:
    try:
        proc = subprocess.run(  # noqa: S603 - args are constructed internally, not shell
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        # A missing binary must look like a failed command, not an uncaught exception, so
        # callers (scope's gh/git helpers, harness adapters) can degrade gracefully.
        return ProcResult(_CMD_NOT_FOUND, "", f"{args[0]}: command not found")
    return ProcResult(proc.returncode, proc.stdout, proc.stderr)


async def run_async(
    args: list[str],
    cwd: Path | None = None,
    log_path: Path | None = None,
    input_text: str | None = None,
) -> ProcResult:
    """Run a command, capturing stdout/stderr. Optionally tee raw output to a log file.

    Harness CLIs buffer their output and only surface it on exit, so there is no value
    in streaming line-by-line here; we capture fully, then persist the raw bytes.

    `input_text` is fed on stdin — the way harness CLIs take large prompts without
    risking an ARG_MAX overflow from a giant argv element.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        # A missing harness binary becomes a failed result the adapter turns into an
        # AdapterError, so one absent CLI fails just that review instead of crashing the run.
        return ProcResult(_CMD_NOT_FOUND, "", f"{args[0]}: command not found")
    stdin_b = input_text.encode("utf-8") if input_text is not None else None
    stdout_b, stderr_b = await proc.communicate(input=stdin_b)
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if log_path is not None:
        log_path.write_text(stdout + ("\n--- stderr ---\n" + stderr if stderr else ""), "utf-8")
    return ProcResult(proc.returncode or 0, stdout, stderr)
