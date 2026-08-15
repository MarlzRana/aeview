"""Prompt-embedded JSON Schema: embed, extract, re-prompt suffix.

Shared by harnesses whose schema_support is "prompt" (copilot, pi): the wanted JSON Schema is
appended to the prompt and parsed back out of free-form model text. Bounded scan so a
pathological brace-heavy answer can't block the event loop.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Iterator

# Prompt-only can't guarantee conformance, so adapters re-prompt once on invalid output, then fail.
MAX_ATTEMPTS = 2

# The retry turn rides the SAME session, so the model still sees its first (bad) answer + the schema
# above it — this just re-states the format requirement more forcefully.
RETRY_SUFFIX = (
    "Respond with ONLY the JSON object described above — no prose, no explanation, no markdown "
    "fence, nothing before or after the object."
)

# Bound each decode to a window slice and cap the number of candidate `{` starts so a pathological
# brace-heavy / unterminated-string response can't make the inline (synchronous) scan block the
# event loop. The window is far larger than any real review/dedup object, so a complete answer is
# never truncated, while one decode can't scan an arbitrarily large output. We bound the per-attempt
# window (a slice) rather than trusting exc.pos — an unterminated string reports the string's start,
# not how far raw_decode scanned.
_MAX_SCAN_CHARS = 1_000_000
_MAX_JSON_STARTS = 256

_DECODER = json.JSONDecoder()


def embed_schema(prompt: str, schema: dict) -> str:
    return (
        f"{prompt}\n\n"
        f"## Required output format\n\n"
        f"Respond with a single JSON object conforming exactly to this JSON Schema. Output ONLY "
        f"that JSON object — no prose, no explanation, no markdown fence.\n\n"
        f"```json\n{json.dumps(schema)}\n```\n"
    )


def extract_json(answer: str, schema: dict) -> dict | None:
    """Pull the schema-conforming object out of a prompt-only answer.

    Scan each `{` with json.raw_decode — the real parser, so it handles strings/escapes/nesting
    correctly and ignores trailing prose/fences — and keep the first object that matches the schema.
    A prompt-only model may wrap the answer (e.g. {"output": {...}}); keep the first nested match as
    a fallback. We retain only one candidate, so a many-object response can't spike memory.
    """
    required = set(schema.get("required", []))
    properties = set(schema.get("properties", {}))
    nested_fallback: dict | None = None
    for obj in json_objects(answer):
        if matches(obj, required, properties):
            return obj
        if nested_fallback is None:
            nested_fallback = find_nested_match(obj, required, properties)
    return nested_fallback


def find_nested_match(value: object, required: set[str], properties: set[str]) -> dict | None:
    """Breadth-first search of a parsed value's nested dicts/lists for the first schema-matching
    object. Iterative (not recursive) so a deeply nested parsed object can't hit Python's recursion
    limit and crash extraction."""
    queue: deque[object] = deque([value])
    while queue:
        current = queue.popleft()
        if isinstance(current, dict):
            children: Iterable[object] = current.values()
        elif isinstance(current, list):
            children = current
        else:
            continue
        # One pass: match each child, else enqueue it for deeper search (no copy / second scan).
        for child in children:
            if isinstance(child, dict) and matches(child, required, properties):
                return child
            if isinstance(child, (dict, list)):
                queue.append(child)
    return None


def json_objects(text: str) -> Iterator[dict]:
    """Yield each parseable JSON object in `text`, in document order, via raw_decode.

    Each candidate `{` is decoded from a bounded window slice — so even the first/clean decode can't
    scan an arbitrarily large output inline — advancing past each parsed object so its interior
    braces aren't rescanned. The window dwarfs any real answer (no truncation); the start
    cap bounds the pathological brace-heavy case.
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
    # raw_decode at a `{` yields an object (dict) or raises JSONDecodeError. The C scanner does not
    # raise RecursionError even on very deep nesting (it reports a JSONDecodeError at the
    # unterminated end instead), so JSONDecodeError is the only failure to handle here.
    try:
        obj, end = _DECODER.raw_decode(window, 0)
    except json.JSONDecodeError:
        return None
    return obj, end


def matches(obj: object, required: set[str], properties: set[str]) -> bool:
    if not isinstance(obj, dict) or not required <= obj.keys():
        return False
    # An all-defaulted schema (no required keys, e.g. DuplicateGroups) would otherwise accept a
    # stray `{}` before the real answer. Demand at least one of the schema's own properties — not
    # all of them, so a payload that legitimately omits optional fields still matches.
    if not required and properties:
        return bool(obj.keys() & properties)
    return True
