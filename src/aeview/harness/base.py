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


class AdapterError(Exception):
    """A harness invocation failed.

    `transient` marks failures worth retrying with backoff (rate-limit, overload, timeout);
    everything else (bad auth, missing binary, schema-invalid output) fails fast.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class HarnessOutput:
    """Parsed result of one harness invocation."""

    def __init__(self, review: ReviewOutput, usage: Usage, raw: str) -> None:
        self.review = review
        self.usage = usage
        self.raw = raw


class Adapter(Protocol):
    name: str
    schema_support: SchemaSupport

    async def run(self, prompt: str, model: str, cwd: Path, log_path: Path) -> HarnessOutput: ...


def get_adapter(harness: str) -> Adapter:
    # Lazy import keeps the adapter registry flat and avoids import cycles.
    from .claude_code import ClaudeCodeAdapter

    adapters: dict[str, Adapter] = {
        "claude-code": ClaudeCodeAdapter(),
    }
    if harness not in adapters:
        raise AdapterError(
            f"harness '{harness}' is not supported in this build; available: "
            f"{', '.join(sorted(adapters))}"
        )
    return adapters[harness]
