"""copilot adapter: prompt-embedded schema, blocklist read-only, JSONL output parsing.

Copilot has no schema flag (`schema_support="prompt"`): the wanted JSON Schema is appended to
the prompt with a strict "return ONLY this JSON" instruction, and aeview validates + re-prompts
once on invalid output — the one place `schema_support` drives the reaction.

Output is `--output-format json` = JSONL events; the model's answer is the `data.content` of the
final `assistant.message` event (a `result` event closes the stream). Copilot reports no input
tokens and no USD cost, so usage carries only output tokens (like codex).

Read-only is a blocklist, not an allowlist, so new read tools auto-enable without a config
change: `--allow-all-tools` runs every tool without a prompt (headless-safe), while `--deny-tool`
blocks the write vectors (`write`, `shell`) and network (`url`) — denial takes precedence over
`--allow-all-tools`. Mutating tools also need approval that can't be granted headlessly, a
backstop for any novel kind. (Copilot's native local sandbox has no per-invocation toggle yet;
revisit when it does — see the implementation log's deferred item.)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from ..process import run_async
from ..schema import ReviewOutput, Usage, review_output_json_schema
from .base import AdapterError, HarnessOutput, SchemaSupport, StructuredOutput, looks_transient

# copilot reasoning-effort levels (--effort); "default"/None -> leave unset.
_EFFORT_LEVELS = {"none", "low", "medium", "high", "xhigh", "max"}

# Prompt-only can't guarantee conformance, so re-prompt once on invalid output, then fail.
_MAX_ATTEMPTS = 2

_READ_ONLY_ARGS = [
    "--output-format", "json",
    "--stream", "off",
    "--allow-all-tools",
    "--deny-tool=write",
    "--deny-tool=shell",
    "--deny-tool=url",
    "--disable-builtin-mcps",
    "--no-ask-user",
]

_RETRY_SUFFIX = (
    "\n\nYour previous response was not valid JSON matching the schema. Respond with ONLY the "
    "JSON object — no prose, no explanation, no markdown fence."
)


class CopilotAdapter:
    name: str = "copilot"
    schema_support: SchemaSupport = "prompt"
    binary: str = "copilot"
    auth_status_args: list[str] = []  # no no-cost auth probe; doctor warns  # noqa: RUF012

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
        base_prompt = _embed_schema(prompt, schema)
        args = ["copilot", *_READ_ONLY_ARGS]
        if model:
            args += ["--model", model]
        if thinking and thinking != "default":
            if thinking not in _EFFORT_LEVELS:
                raise AdapterError(
                    f"copilot thinking '{thinking}' invalid; use one of {sorted(_EFFORT_LEVELS)}"
                )
            args += ["--effort", thinking]

        last_error = "copilot produced no valid output"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            text = base_prompt if attempt == 1 else base_prompt + _RETRY_SUFFIX
            res = await run_async(
                args, cwd=cwd, log_path=log_path, input_text=text, timeout=timeout
            )
            if res.returncode != 0:
                # A non-zero exit is a hard failure (bad auth, crash, timeout) — re-prompting
                # won't help, so fail fast (transient-aware so the fan-out can retry overload).
                detail = res.stderr.strip() or res.stdout.strip() or "no output"
                raise AdapterError(
                    f"copilot exited {res.returncode}: {detail}", transient=looks_transient(detail)
                )
            payload = _extract_json(res.stdout, schema)
            if payload is not None:
                return StructuredOutput(payload=payload, usage=_usage(res.stdout), raw=res.stdout)
            last_error = "copilot did not return a JSON object matching the schema"
        raise AdapterError(last_error)

    async def run(
        self, prompt: str, model: str, cwd: Path, log_path: Path, thinking: str | None = None
    ) -> HarnessOutput:
        out = await self.run_structured(
            prompt, review_output_json_schema(), model, cwd, log_path, thinking
        )
        try:
            review = ReviewOutput.model_validate(out.payload)
        except ValidationError as exc:
            raise AdapterError(f"copilot output failed schema validation: {exc}") from exc
        return HarnessOutput(review=review, usage=out.usage, raw=out.raw)


def _embed_schema(prompt: str, schema: dict) -> str:
    return (
        f"{prompt}\n\n"
        f"## Required output format\n\n"
        f"Respond with a single JSON object conforming exactly to this JSON Schema. Output ONLY "
        f"that JSON object — no prose, no explanation, no markdown fence.\n\n"
        f"```json\n{json.dumps(schema)}\n```\n"
    )


def _extract_json(stdout: str, schema: dict) -> dict | None:
    """Pull the schema-conforming object out of copilot's JSONL stream.

    The answer is the final `assistant.message`'s `data.content`; we try that first (raw, then
    fenced, then a brace-matched span), falling back to scanning the whole stream.
    """
    required = set(schema.get("required", []))
    sources = [c for c in (_final_assistant_content(stdout),) if c]
    sources.append(stdout)  # fallback: scan the raw stream
    for text in sources:
        for candidate in _json_candidates(text):
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and required <= obj.keys():
                return obj
    return None


def _final_assistant_content(stdout: str) -> str | None:
    content: str | None = None
    for event in _events(stdout):
        if event.get("type") == "assistant.message":
            data = event.get("data")
            if isinstance(data, dict) and isinstance(data.get("content"), str):
                content = data["content"]  # keep the last one
    return content


def _json_candidates(text: str) -> Iterator[str]:
    text = text.strip()
    yield text
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        yield match.group(1).strip()
    yield from _brace_spans(text)


def _brace_spans(text: str) -> Iterator[str]:
    """Top-level {...} spans — a last-resort way to find an object embedded in prose.

    String/escape aware: braces inside JSON string values (e.g. a finding body containing code)
    must not move the depth counter, or the span would close early and the object misparse.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start : i + 1]


def _usage(stdout: str) -> Usage:
    """Sum output tokens across assistant.message events; copilot reports no input/USD cost."""
    output_tokens = 0
    for event in _events(stdout):
        if event.get("type") == "assistant.message":
            data = event.get("data")
            if isinstance(data, dict):
                output_tokens += int(data.get("outputTokens", 0) or 0)
    return Usage(input_tokens=0, output_tokens=output_tokens, cost_usd=0.0)


def _events(stdout: str) -> Iterator[dict]:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event
