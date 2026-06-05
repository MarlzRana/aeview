"""claude-code adapter: structured output via --json-schema, read-only tool set.

The `claude` CLI in --print mode returns a single JSON object. With --json-schema it
puts the schema-conforming object in `structured_output`; cost/token usage live in
`total_cost_usd` and `usage`. We restrict tools to a read-only set so the reviewer can
read context but never mutate the repo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from ..process import run_async
from ..schema import ReviewOutput, Usage, review_output_json_schema
from .base import AdapterError, HarnessOutput, SchemaSupport

# Read-only built-in tools. No Bash/Edit/Write -> the reviewer cannot mutate the repo.
READ_ONLY_TOOLS = "Read,Grep,Glob"


class ClaudeCodeAdapter:
    name: str = "claude-code"
    schema_support: SchemaSupport = "validated"

    async def run(self, prompt: str, model: str, cwd: Path, log_path: Path) -> HarnessOutput:
        schema = json.dumps(review_output_json_schema())
        args = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            model,
            "--json-schema",
            schema,
            "--tools",
            READ_ONLY_TOOLS,
            "--no-session-persistence",
        ]
        env_cwd = cwd if cwd.exists() else Path(os.getcwd())
        res = await run_async(args, cwd=env_cwd, log_path=log_path)
        if res.returncode != 0:
            raise AdapterError(
                f"claude exited {res.returncode}: {res.stderr.strip() or res.stdout.strip()}"
            )
        return self._parse(res.stdout)

    def _parse(self, stdout: str) -> HarnessOutput:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"claude output was not JSON: {exc}") from exc

        if payload.get("is_error"):
            status = payload.get("api_error_status") or payload.get("result") or "unknown error"
            raise AdapterError(f"claude reported an error: {status}")

        structured = payload.get("structured_output")
        if structured is None:
            raise AdapterError("claude output had no structured_output (schema not honored)")

        try:
            review = ReviewOutput.model_validate(structured)
        except ValidationError as exc:
            raise AdapterError(f"claude output failed schema validation: {exc}") from exc

        usage = self._usage(payload)
        return HarnessOutput(review=review, usage=usage, raw=stdout)

    @staticmethod
    def _usage(payload: dict) -> Usage:
        u = payload.get("usage") or {}
        return Usage(
            input_tokens=int(u.get("input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cost_usd=float(payload.get("total_cost_usd", 0.0) or 0.0),
        )
