"""Adapter protocol: each harness owns how it delivers the schema and runs read-only.

`schema_support` declares the capability so the orchestrator never branches on harness
names:
- "constrained": the harness enforces the schema during decoding (codex --output-schema)
- "validated":   the harness validates-and-reprompts against the schema (claude --json-schema)
- "prompt":      the schema is embedded in the prompt only (copilot); aeview must parse it out

aeview post-validates every output regardless, so a drifting harness fails loudly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from ..schema import ReviewOutput, Usage

SchemaSupport = Literal["constrained", "validated", "prompt"]

# Text fragments that signal a transient (retry-worthy) failure, shared across adapters.
_TRANSIENT_TEXT = ("rate limit", "overloaded", "capacity", "timeout", "timed out", "try again")


def looks_transient(text: str) -> bool:
    low = text.lower()
    return any(frag in low for frag in _TRANSIENT_TEXT)


class AdapterError(Exception):
    """A harness invocation failed.

    `transient` marks failures worth retrying with backoff (rate-limit, overload, timeout);
    everything else (bad auth, missing binary, schema-invalid output) fails fast.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class HarnessOutput:
    """Parsed result of one review invocation."""

    def __init__(self, review: ReviewOutput, usage: Usage, raw: str) -> None:
        self.review = review
        self.usage = usage
        self.raw = raw


class StructuredOutput:
    """Result of a generic schema-constrained invocation: the conforming JSON + usage.

    `payload` is validated against the *caller's* schema (ReviewOutput for a review,
    DuplicateGroups for dedup) by the caller; the adapter only guarantees it is the JSON the
    harness produced under the given schema.
    """

    def __init__(self, payload: dict, usage: Usage, raw: str) -> None:
        self.payload = payload
        self.usage = usage
        self.raw = raw


class Adapter(Protocol):
    name: str
    schema_support: SchemaSupport
    binary: str  # the CLI executable, for doctor's PATH check
    auth_status_args: list[str]  # a no-cost auth/status command; rc==0 means authenticated

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
        """Invoke read-only under an arbitrary JSON Schema; the adapter owns schema delivery."""
        ...

    async def run(
        self, prompt: str, model: str, cwd: Path, log_path: Path, thinking: str | None = None
    ) -> HarnessOutput: ...


def _registry() -> dict[str, Adapter]:
    # Lazy import keeps the adapter registry flat and avoids import cycles.
    from .claude_code import ClaudeCodeAdapter
    from .codex import CodexAdapter
    from .copilot import CopilotAdapter

    return {
        "claude-code": ClaudeCodeAdapter(),
        "codex": CodexAdapter(),
        "copilot": CopilotAdapter(),
    }


def get_adapter(harness: str) -> Adapter:
    adapters = _registry()
    if harness not in adapters:
        raise AdapterError(
            f"harness '{harness}' is not supported in this build; available: "
            f"{', '.join(sorted(adapters))}"
        )
    return adapters[harness]
