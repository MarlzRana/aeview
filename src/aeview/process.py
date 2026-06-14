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
TIMED_OUT = 124  # conventional shell exit code for a timed-out command


def _spawn_failure(args: list[str], cwd: Path | None) -> ProcResult:
    """Turn a spawn FileNotFoundError into a failed result with the *right* cause.

    `subprocess`/`create_subprocess_exec` raise the same FileNotFoundError whether the
    executable is missing or `cwd` does not exist; disambiguate so we never blame the
    binary for a bad working directory.
    """
    if cwd is not None and not Path(cwd).exists():
        return ProcResult(_CMD_NOT_FOUND, "", f"working directory not found: {cwd}")
    return ProcResult(_CMD_NOT_FOUND, "", f"{args[0]}: command not found")


def run_sync(args: list[str], cwd: Path | None = None, timeout: float | None = None) -> ProcResult:
    try:
        proc = subprocess.run(  # noqa: S603 - args are constructed internally, not shell
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        # A missing binary/cwd must look like a failed command, not an uncaught exception,
        # so callers (scope's gh/git helpers, harness adapters) can degrade gracefully.
        return _spawn_failure(args, cwd)
    except subprocess.TimeoutExpired:
        # A wedged command (e.g. a hanging auth probe) becomes a failed result, not a hang.
        return ProcResult(TIMED_OUT, "", f"{args[0]}: timed out after {timeout}s")
    except OSError as exc:
        # Any other spawn failure (e.g. PermissionError when a binary override points at a
        # non-executable file) is a failed command, not a crash — doctor probes it via a path.
        return ProcResult(_CMD_NOT_FOUND, "", f"{args[0]}: {exc}")
    return ProcResult(proc.returncode, proc.stdout, proc.stderr)


async def run_async(
    args: list[str],
    cwd: Path | None = None,
    log_path: Path | None = None,
    input_text: str | None = None,
    timeout: float | None = None,
) -> ProcResult:
    """Run a command, capturing stdout/stderr. Optionally tee raw output to a log file.

    Harness CLIs buffer their output and only surface it on exit, so there is no value
    in streaming line-by-line here; we capture fully, then persist the raw bytes.

    `input_text` is fed on stdin — the way harness CLIs take large prompts without
    risking an ARG_MAX overflow from a giant argv element.

    `timeout` (seconds) bounds the call: on expiry the child is killed and a 124 result is
    returned, which the adapter turns into a failure. (Killing only the direct child, not its
    process group — a SIGTERM-deaf harness can orphan tool grandchildren; full process-group
    kill is a deferred stretch item, see the roadmap.)
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
        # A missing harness binary/cwd becomes a failed result the adapter turns into an
        # AdapterError, so one absent CLI fails just that review instead of crashing the run.
        return _spawn_failure(args, cwd)
    except OSError as exc:
        # Other spawn failures (e.g. a non-executable binary override) also degrade to a failed
        # result rather than crashing the fan-out.
        return ProcResult(_CMD_NOT_FOUND, "", f"{args[0]}: {exc}")
    stdin_b = input_text.encode("utf-8") if input_text is not None else None
    try:
        stdout_b, stderr_b = await _communicate(proc, stdin_b, timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        msg = f"{args[0]}: timed out after {timeout}s"
        if log_path is not None:
            log_path.write_text(f"--- stderr ---\n{msg}", "utf-8")
        return ProcResult(TIMED_OUT, "", msg)
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if log_path is not None:
        log_path.write_text(stdout + ("\n--- stderr ---\n" + stderr if stderr else ""), "utf-8")
    return ProcResult(proc.returncode or 0, stdout, stderr)


async def _communicate(
    proc: asyncio.subprocess.Process, stdin_b: bytes | None, timeout: float | None
) -> tuple[bytes, bytes]:
    if timeout is None:
        return await proc.communicate(input=stdin_b)
    return await asyncio.wait_for(proc.communicate(input=stdin_b), timeout=timeout)
