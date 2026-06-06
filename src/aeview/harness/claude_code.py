"""claude-code adapter: structured output via --json-schema, native read-only sandbox.

Read-only is enforced by Claude Code's OS-level sandbox plus permission policy, NOT a
tool allowlist — so the reviewer can freely use read tools (rg, read-only git) while
every mutation is blocked. Verified behavior: reads/`git diff` run without prompts,
writes are blocked at both the tool layer and the OS sandbox, and the run never hangs.

- `--permission-mode dontAsk`: auto-runs read-only bash, auto-denies anything that would
  otherwise prompt (writes, network) — fail-closed and non-interactive-safe.
- sandbox `denyWrite: ["/"]`: no filesystem writes anywhere, including the cwd.
- `--disallowedTools Edit Write NotebookEdit`: the built-in mutating tools bypass the
  sandbox (they use the permission system), so they are removed from the model entirely.
- prompt is fed on stdin to avoid an ARG_MAX overflow on large inline diffs.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from ..process import run_async
from ..schema import ReviewOutput, Usage, review_output_json_schema
from .base import AdapterError, HarnessOutput, SchemaSupport

_SANDBOX_SETTINGS = json.dumps(
    {
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {"denyWrite": ["/"]},
        }
    }
)

_DISALLOWED_TOOLS = "Edit Write NotebookEdit"

# HTTP statuses worth retrying, and text fragments that signal a transient failure.
_TRANSIENT_STATUS = {429, 500, 502, 503, 529}
_TRANSIENT_TEXT = ("rate limit", "overloaded", "capacity", "timeout", "timed out", "try again")


def _looks_transient(text: str) -> bool:
    low = text.lower()
    return any(frag in low for frag in _TRANSIENT_TEXT)


class ClaudeCodeAdapter:
    name: str = "claude-code"
    schema_support: SchemaSupport = "validated"

    async def run(self, prompt: str, model: str, cwd: Path, log_path: Path) -> HarnessOutput:
        schema = json.dumps(review_output_json_schema())
        args = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--json-schema",
            schema,
            "--permission-mode",
            "dontAsk",
            "--disallowedTools",
            _DISALLOWED_TOOLS,
            "--settings",
            _SANDBOX_SETTINGS,
            "--no-session-persistence",
        ]
        res = await run_async(args, cwd=cwd, log_path=log_path, input_text=prompt)
        return self._interpret(res.stdout, res.stderr, res.returncode)

    def _interpret(self, stdout: str, stderr: str, returncode: int) -> HarnessOutput:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            # No parseable result: a non-zero exit is the failure (e.g. missing binary, crash).
            detail = stderr.strip() or stdout.strip() or "no output"
            raise AdapterError(
                f"claude exited {returncode}: {detail}", transient=_looks_transient(detail)
            ) from None

        if payload.get("is_error"):
            status = payload.get("api_error_status")
            text = str(payload.get("result") or "")
            transient = status in _TRANSIENT_STATUS or _looks_transient(text)
            raise AdapterError(f"claude reported an error: {status or text}", transient=transient)

        if returncode != 0:
            raise AdapterError(f"claude exited {returncode} without an error payload")

        structured = payload.get("structured_output")
        if structured is None:
            raise AdapterError("claude output had no structured_output (schema not honored)")

        try:
            review = ReviewOutput.model_validate(structured)
        except ValidationError as exc:
            raise AdapterError(f"claude output failed schema validation: {exc}") from exc

        return HarnessOutput(review=review, usage=self._usage(payload), raw=stdout)

    @staticmethod
    def _usage(payload: dict) -> Usage:
        u = payload.get("usage") or {}
        return Usage(
            input_tokens=int(u.get("input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cost_usd=float(payload.get("total_cost_usd", 0.0) or 0.0),
        )
