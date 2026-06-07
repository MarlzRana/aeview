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
from collections import deque
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
        """`validate` (optional) is a deep schema check (e.g. ReviewOutput.model_validate) used
        by the review path (`run`): it raises on a structurally-present-but-invalid payload
        (wrong enum/type) so that case re-prompts too. The generic dedup caller intentionally
        omits it and one-shots — a deep-invalid dedup payload degrades to the raw-union path."""
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


# Bound each decode to a window slice and cap the number of candidate `{` starts so a
# pathological brace-heavy / unterminated-string response can't make the inline (synchronous)
# scan block the shared event loop. The window is far larger than any real review/dedup object,
# so a complete answer is never truncated, while one decode can't scan an arbitrarily large
# output. We bound the per-attempt window (a slice) rather than trusting exc.pos — an
# unterminated string reports the string's start, not how far raw_decode scanned.
_MAX_SCAN_CHARS = 1_000_000
_MAX_JSON_STARTS = 256

_DECODER = json.JSONDecoder()


def _extract_json(stdout: str, schema: dict) -> dict | None:
    """Pull the schema-conforming object out of copilot's JSONL stream.

    The answer is the final `assistant.message`'s `data.content` (tried first, then the raw
    stream). Within a source, scan each `{` with json.raw_decode — the real parser, so it
    handles strings/escapes/nesting correctly and ignores trailing prose/fences — and keep the
    first object that matches the schema.
    """
    required = set(schema.get("required", []))
    properties = set(schema.get("properties", {}))
    content = _final_assistant_content(stdout)
    sources = [content, stdout] if content else [stdout]  # fall back to scanning the raw stream
    # A top-level match wins. Failing that, a prompt-only model may have wrapped the answer
    # (e.g. {"output": {...}}); keep the first nested match as a fallback. We don't retain every
    # parsed object — only one candidate — so a many-object response can't spike memory.
    nested_fallback: dict | None = None
    for text in sources:
        for obj in _json_objects(text):
            if _matches(obj, required, properties):
                return obj
            if nested_fallback is None:
                nested_fallback = _find_nested_match(obj, required, properties)
    return nested_fallback


def _find_nested_match(value: object, required: set[str], properties: set[str]) -> dict | None:
    """Breadth-first search of a parsed value's nested dicts/lists for the first schema-matching
    object. Iterative (not recursive) so a deeply nested parsed object can't hit Python's
    recursion limit and crash extraction."""
    queue: deque[object] = deque([value])
    while queue:
        current = queue.popleft()
        if isinstance(current, dict):
            children: list[object] = list(current.values())
        elif isinstance(current, list):
            children = current
        else:
            continue
        for child in children:
            if isinstance(child, dict) and _matches(child, required, properties):
                return child
        queue.extend(c for c in children if isinstance(c, (dict, list)))
    return None


def _json_objects(text: str) -> Iterator[dict]:
    """Yield each parseable JSON object in `text`, in document order, via raw_decode.

    Each candidate `{` is decoded from a bounded window slice — so even the first/clean decode
    can't scan an arbitrarily large output inline — advancing past each parsed object so its
    interior braces aren't rescanned. The window dwarfs any real answer (no truncation in
    practice); the start cap bounds the pathological brace-heavy case.
    """
    i = 0
    for _ in range(_MAX_JSON_STARTS):
        start = text.find("{", i)
        if start == -1:
            return
        decoded = _decode(text[start : start + _MAX_SCAN_CHARS])
        if decoded is None:
            i = start + 1  # not a valid object start (e.g. a brace in prose) — try the next `{`
            continue
        obj, length = decoded
        i = start + length  # skip the whole object; don't rescan its interior braces
        yield obj


def _decode(window: str) -> tuple[dict, int] | None:
    # raw_decode at a `{` yields an object (dict) or raises JSONDecodeError. The C scanner does
    # not raise RecursionError even on very deep nesting (it reports a JSONDecodeError at the
    # unterminated end instead), so JSONDecodeError is the only failure to handle here.
    try:
        obj, end = _DECODER.raw_decode(window, 0)
    except json.JSONDecodeError:
        return None
    return obj, end


def _matches(obj: object, required: set[str], properties: set[str]) -> bool:
    if not isinstance(obj, dict) or not required <= obj.keys():
        return False
    # An all-defaulted schema (no required keys, e.g. DuplicateGroups) would otherwise accept a
    # stray `{}` before the real answer. Demand at least one of the schema's own properties —
    # not all of them, so a payload that legitimately omits optional fields still matches.
    if not required and properties:
        return bool(obj.keys() & properties)
    return True


def _final_assistant_content(stdout: str) -> str | None:
    content: str | None = None
    for event in _events(stdout):
        if event.get("type") == "assistant.message":
            data = event.get("data")
            if isinstance(data, dict) and isinstance(data.get("content"), str):
                content = data["content"]  # keep the last one
    return content


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
