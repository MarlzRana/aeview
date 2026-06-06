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
from collections.abc import Callable, Iterator
from pathlib import Path

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

# Each attempt is a fresh stateless process, so this can't reference a "previous response" —
# it just re-states the format requirement more forcefully.
_RETRY_SUFFIX = (
    "\n\nIMPORTANT: Respond with ONLY the JSON object described above — no prose, no explanation, "
    "no markdown fence, nothing before or after the object."
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
        validate: Callable[[dict], object] | None = None,
    ) -> StructuredOutput:
        """`validate` (optional) is the deep schema check (e.g. ReviewOutput.model_validate); it
        raises on a structurally-present-but-invalid payload (wrong enum/type) so that case also
        re-prompts, instead of slipping past the cheap key check to fail later in the caller."""
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
        output_tokens = 0  # accumulate across attempts so a re-prompt isn't under-counted
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
            output_tokens += _output_tokens(res.stdout)
            payload = _extract_json(res.stdout, schema)
            if payload is None:
                last_error = "copilot did not return a JSON object matching the schema"
                continue
            if validate is not None:
                try:
                    validate(payload)
                except Exception as exc:  # noqa: BLE001 - any validation failure should re-prompt
                    last_error = f"copilot output failed schema validation: {exc}"
                    continue
            usage = Usage(input_tokens=0, output_tokens=output_tokens, cost_usd=0.0)
            return StructuredOutput(payload=payload, usage=usage, raw=res.stdout)
        raise AdapterError(last_error)

    async def run(
        self, prompt: str, model: str, cwd: Path, log_path: Path, thinking: str | None = None
    ) -> HarnessOutput:
        out = await self.run_structured(
            prompt, review_output_json_schema(), model, cwd, log_path, thinking,
            validate=ReviewOutput.model_validate,
        )
        review = ReviewOutput.model_validate(out.payload)
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
    # The cheap structural check: an object must carry the schema's required keys. When a schema
    # has NO required keys (e.g. DuplicateGroups, all-defaulted), that test is vacuous and would
    # accept a stray `{}` before the real answer — so then require the property keys instead.
    required = set(schema.get("required", [])) or set(schema.get("properties", {}))
    content = _final_assistant_content(stdout)
    sources = [content, stdout] if content else [stdout]  # fall back to scanning the raw stream
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
        # Only track string state *inside* an object: a stray quote in the prose before the
        # object (e.g. `the "fix":`) must not flip in_string and swallow the opening brace.
        if depth == 0:
            if ch == "{":
                start = i
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                yield text[start : i + 1]


def _output_tokens(stdout: str) -> int:
    """Sum output tokens across assistant.message events; copilot reports no input/USD cost."""
    return sum(
        int(event.get("data", {}).get("outputTokens", 0) or 0)
        for event in _events(stdout)
        if event.get("type") == "assistant.message" and isinstance(event.get("data"), dict)
    )


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
