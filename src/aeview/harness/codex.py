"""codex adapter: schema-constrained final message, native read-only sandbox.

codex enforces the output schema during decoding (`--output-schema`), so its final message
is the schema-conforming JSON. We capture that via `--output-last-message <file>` and read
token usage from the `turn.completed` JSONL event (codex reports no USD cost). Read-only is
codex's native `--sandbox read-only` (rg / read-only git still work; writes/network blocked);
`approval_policy="never"` keeps the non-interactive run from escalating.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pydantic import ValidationError

from ..process import run_async
from ..schema import ReviewOutput, Usage, make_strict_schema, review_output_json_schema
from .base import (
    AdapterError,
    HarnessOutput,
    SchemaSupport,
    StructuredOutput,
    classify_transient,
)

# codex reasoning-effort levels (no "minimal", unlike claude); "default"/None -> leave unset.
_EFFORT_LEVELS = {"low", "medium", "high", "xhigh"}


class CodexAdapter:
    name: str = "codex"
    schema_support: SchemaSupport = "constrained"
    binary: str = "codex"
    auth_status_args: list[str] = ["codex", "login", "status"]  # noqa: RUF012

    async def run_structured(
        self,
        prompt: str,
        schema: dict,
        model: str,
        cwd: Path,
        log_path: Path,
        thinking: str | None = None,
        timeout: float | None = None,
    ) -> StructuredOutput:
        with tempfile.TemporaryDirectory(prefix="aeview-codex-") as tmp:
            schema_file = Path(tmp) / "schema.json"
            # codex's constrained decoding (OpenAI strict mode) needs every property required.
            schema_file.write_text(json.dumps(make_strict_schema(schema)), encoding="utf-8")
            last_message = Path(tmp) / "last_message.txt"

            args = [
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "-c",
                'approval_policy="never"',
                "--model",
                model,
                "--output-schema",
                str(schema_file),
                "--output-last-message",
                str(last_message),
                "--ephemeral",
                "--json",
            ]
            if thinking and thinking != "default":
                if thinking not in _EFFORT_LEVELS:
                    raise AdapterError(
                        f"codex thinking '{thinking}' invalid; use one of {sorted(_EFFORT_LEVELS)}"
                    )
                args += ["-c", f'model_reasoning_effort="{thinking}"']

            res = await run_async(
                args, cwd=cwd, log_path=log_path, input_text=prompt, timeout=timeout
            )
            final = last_message.read_text(encoding="utf-8") if last_message.exists() else ""

        return self._interpret(final, res.stdout, res.stderr, res.returncode)

    async def run(
        self,
        prompt: str,
        model: str,
        cwd: Path,
        log_path: Path,
        thinking: str | None = None,
        timeout: float | None = None,
    ) -> HarnessOutput:
        out = await self.run_structured(
            prompt, review_output_json_schema(), model, cwd, log_path, thinking, timeout
        )
        try:
            review = ReviewOutput.model_validate(out.payload)
        except ValidationError as exc:
            raise AdapterError(f"codex output failed schema validation: {exc}") from exc
        return HarnessOutput(review=review, usage=out.usage, raw=out.raw)

    def _interpret(self, final: str, stdout: str, stderr: str, returncode: int) -> StructuredOutput:
        # codex exits 0 on success; a non-zero exit means the run failed, so never trust a
        # final message it may have left behind — treat the whole invocation as a failure.
        if returncode != 0:
            detail = _error_detail(stdout, stderr)
            raise AdapterError(
                f"codex exited {returncode}: {detail}",
                transient=classify_transient(returncode, detail),
            )
        if not final.strip():
            raise AdapterError("codex produced no final message")

        try:
            payload = json.loads(final)
        except json.JSONDecodeError as exc:
            # --output-schema constrains decoding, so a non-JSON final message is a hard failure.
            raise AdapterError(f"codex final message was not JSON: {exc}") from exc

        return StructuredOutput(payload=payload, usage=_usage_from_jsonl(stdout), raw=final)


def _event_type(event: dict) -> str | None:
    """The event type, whether codex puts it at top level or under a `msg` wrapper."""
    direct = event.get("type")
    if direct:
        return direct
    msg = event.get("msg")
    return msg.get("type") if isinstance(msg, dict) else None


def _event_message(event: dict) -> str | None:
    """An error message from an event, checking both the top level and a `msg` wrapper."""
    msg = event.get("msg")
    for source in (event, msg if isinstance(msg, dict) else {}):
        if source.get("message"):
            return str(source["message"])
        err = source.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return None


def _error_detail(stdout: str, stderr: str) -> str:
    """Prefer a codex error/turn.failed message from the JSONL stream over noisy stderr."""
    message: str | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and _event_type(event) in ("error", "turn.failed"):
            message = _event_message(event) or message
    return (message or stderr.strip() or "codex failed").strip()


def _usage_from_jsonl(stdout: str) -> Usage:
    """Sum token usage across `turn.completed` events in codex's JSONL stream (no USD cost)."""
    input_tokens = 0
    output_tokens = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = _event_usage(event)
        if usage is not None:
            input_tokens += int(usage.get("input_tokens", 0) or 0)
            output_tokens += int(usage.get("output_tokens", 0) or 0)
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=0.0)


def _event_usage(event: dict) -> dict | None:
    if not isinstance(event, dict) or _event_type(event) != "turn.completed":
        return None
    msg = event.get("msg")
    msg = msg if isinstance(msg, dict) else {}
    usage = event.get("usage") or msg.get("usage")
    return usage if isinstance(usage, dict) else None
