"""Adapter protocol: each harness owns how it delivers the schema and runs read-only.

`schema_support` declares the capability so the orchestrator never branches on harness
names:
- "constrained": the harness enforces the schema during decoding (codex --output-schema)
- "validated":   the harness validates-and-reprompts against the schema (claude --json-schema)
- "prompt":      the schema is embedded in the prompt only (copilot); aeview must parse it out

aeview post-validates every output regardless, so a drifting harness fails loudly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Literal, Protocol

from ..process import TIMED_OUT, run_sync
from ..schema import ReviewOutput, Usage, build_review_validator, review_output_json_schema

SchemaSupport = Literal["constrained", "validated", "prompt"]

# A no-cost auth/status probe must not hang doctor; bound it.
AUTH_PROBE_TIMEOUT = 10.0

PreflightStatus = Literal["ok", "warn", "fail"]


@dataclass(slots=True)
class Preflight:
    """One adapter's doctor verdict: binary resolvable + (where probeable) authenticated."""

    status: PreflightStatus
    detail: str


# Text fragments that signal a transient (retry-worthy) failure, shared across adapters.
_TRANSIENT_TEXT = ("rate limit", "overloaded", "capacity", "timeout", "timed out", "try again")


def looks_transient(text: str) -> bool:
    low = text.lower()
    return any(frag in low for frag in _TRANSIENT_TEXT)


def classify_transient(returncode: int, detail: str) -> bool:
    """Whether a non-zero harness exit is worth retrying. A per-review timeout (our dedicated
    TIMED_OUT exit) is never retried — fail-fast: one timeout marks the review failed and
    `resume` can re-run it. (Its detail text contains "timed out", which would otherwise read as
    transient.) Every other failure defers to the shared text classifier."""
    return returncode != TIMED_OUT and looks_transient(detail)


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
    binary: str  # the harness binary NAME/path: codex/copilot argv[0]; claude's resolution name
    auth_status_args: list[str]  # no-cost auth-probe subcommand (prepended with binary); [] = none

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
        """Invoke read-only under an arbitrary JSON Schema; the adapter owns schema delivery.
        `validate` (optional) is a deep post-validator the adapter runs on the payload inside its
        logging scope; None one-shots (the dedup caller)."""
        ...

    async def run(
        self,
        prompt: str,
        model: str,
        cwd: Path,
        log_path: Path,
        thinking: str | None = None,
        timeout: float | None = None,
        *,
        schema: dict | None = None,
    ) -> HarnessOutput:
        """Run one review. `schema` is the per-reviewer review JSON Schema (None → the built-in
        default); keyword-only so the dedup path and tests can omit it. Adapters delegate the whole
        body to `run_review` — the only per-harness part is `run_structured`."""
        ...

    def preflight(self) -> Preflight:
        """Doctor check: is this harness's binary resolvable and (where probeable) authed? The
        adapter is constructed via get_adapter() with its settings.overrideHarnessBinaries override
        already applied, so this reads the adapter's own resolved binary — no override arg."""
        ...


async def run_review(
    adapter: Adapter,
    prompt: str,
    model: str,
    cwd: Path,
    log_path: Path,
    thinking: str | None,
    timeout: float | None,
    schema: dict | None,
) -> HarnessOutput:
    """The shared review path every adapter's `run` delegates to. Harness-agnostic: it defaults the
    review schema, derives the matching validator, calls the adapter's own `run_structured` (the one
    per-harness part — schema delivery + read-only), and builds the loose `ReviewOutput` carrier.
    One home for the schema/validator/carrier logic so it can't drift across the four adapters."""
    review_schema = schema or review_output_json_schema()
    out = await adapter.run_structured(
        prompt,
        review_schema,
        model,
        cwd,
        log_path,
        thinking,
        timeout,
        validate=build_review_validator(review_schema),
    )
    return HarnessOutput(
        review=ReviewOutput.model_validate(out.payload), usage=out.usage, raw=out.raw
    )


def default_preflight(adapter: Adapter) -> Preflight:
    """PATH-based check for adapters that invoke a named binary: `adapter.binary` (already the
    override or the default) must be on PATH, then its no-cost auth probe (if any) must succeed.
    Adapters whose SDK resolves a bundled binary not on PATH (claude/codex/copilot) override
    `preflight`. Generic check for a harness invoked by a CLI binary directly (PATH-gated, no
    bundled binary). pi overrides preflight too (it also needs `srt` on PATH); a unit test still
    pins this helper's contract."""
    binary = adapter.binary
    if which(binary) is None:
        return Preflight("fail", f"{binary} not found on PATH")
    if not adapter.auth_status_args:
        return Preflight("warn", f"{binary} present; auth not verifiable")
    probe = [binary, *adapter.auth_status_args]
    if run_sync(probe, timeout=AUTH_PROBE_TIMEOUT).returncode == 0:
        return Preflight("ok", f"{binary} present and authenticated")
    return Preflight("warn", f"{binary} present but auth could not be verified")


def _adapter_classes() -> dict[str, Callable[[str | None], Adapter]]:
    # Lazy import keeps the adapter registry flat and avoids import cycles.
    from .claude_code import ClaudeCodeAdapter
    from .codex import CodexAdapter
    from .copilot import CopilotAdapter
    from .pi import PiAdapter

    return {
        "claude-code": ClaudeCodeAdapter,
        "codex": CodexAdapter,
        "copilot": CopilotAdapter,
        "pi": PiAdapter,
    }


def get_adapter(harness: str, binary_override: str | None = None) -> Adapter:
    classes = _adapter_classes()
    cls = classes.get(harness)
    if cls is None:
        raise AdapterError(
            f"harness '{harness}' is not supported in this build; available: "
            f"{', '.join(sorted(classes))}"
        )
    return cls(binary_override)
