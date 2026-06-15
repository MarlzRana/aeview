"""claude-code adapter: structured output + native read-only sandbox via the Claude Agent SDK.

Runs Claude Code through the `claude-agent-sdk` Python SDK (`query`) instead of shelling out to
the `claude` CLI. The SDK resolves its own bundled `claude` binary by default; a
`settings.overrideHarnessBinaries["claude-code"]` entry overrides it via `cli_path`.

Read-only is the same two-layer defense the CLI path used, preserved byte-for-byte:
- the OS sandbox, passed through the SDK's `settings=` field (an inline JSON string ==
  the `--settings` flag): `filesystem.denyWrite:["/"]` blocks every write, `failIfUnavailable`
  fails closed. The SDK's typed `sandbox=` option cannot express those keys, so the proven block
  rides on `settings=` (do NOT use `extra_args={"settings": ...}` — that emits a duplicate flag).
- `permission_mode="dontAsk"` (auto-runs read-only bash, auto-denies writes/network) +
  `disallowed_tools=["Edit","Write","NotebookEdit"]` (the mutating built-ins bypass the sandbox).

Reads anywhere on disk are allowed (incl. outside cwd); only writes are blocked — the
read-anywhere/write-nowhere contract, live-verified.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from shutil import which
from typing import cast

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    Message,
    ProcessError,
    ResultMessage,
    TextBlock,
    query,
)

from ..process import run_sync
from ..schema import ReviewOutput, Usage, review_output_json_schema
from .base import (
    AUTH_PROBE_TIMEOUT,
    AdapterError,
    HarnessOutput,
    Preflight,
    SchemaSupport,
    StructuredOutput,
    classify_transient,
    looks_transient,
)
from .eventlog import EventLogWriter

# The proven OS-sandbox block, passed via the SDK `settings=` field (inline JSON == --settings).
# denyWrite:["/"] blocks all writes; failIfUnavailable fails closed; the mutating built-in tools
# (Edit/Write/NotebookEdit) bypass the sandbox so they are disallowed separately.
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

_DISALLOWED_TOOLS = ["Edit", "Write", "NotebookEdit"]

# HTTP statuses worth retrying (text-based transients use the shared looks_transient).
_TRANSIENT_STATUS = {429, 500, 502, 503, 529}


class ClaudeCodeAdapter:
    name: str = "claude-code"
    schema_support: SchemaSupport = "validated"
    # The binary NAME (for PATH/bundled resolution in _resolve_cli), not argv[0] — unlike
    # codex/copilot the SDK owns the argv; an override threads to the SDK via cli_path.
    binary: str = "claude"
    auth_status_args: list[str] = ["auth", "status"]  # noqa: RUF012

    def __init__(self, binary_override: str | None = None) -> None:
        # settings.overrideHarnessBinaries["claude-code"], threaded to the SDK as cli_path.
        # None (incl. an empty string) → the SDK's own resolution (bundled binary, then PATH).
        self._cli_path = binary_override or None

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
        # `effort` rides on extra_args (== the `--effort` flag) rather than the typed `effort`
        # field, so a passthrough value matches the CLI exactly and needs no cast. "default"/None
        # leaves it unset.
        extra_args: dict[str, str | None] = {"no-session-persistence": None}
        if thinking and thinking != "default":
            extra_args["effort"] = thinking
        # No system_prompt: compose_prompt is fully self-contained (reviewer body + read-only guard
        # + diff), so the review runs without Claude Code's default agent system prompt — more
        # deterministic for a structured review. (The SDK sends an empty system prompt by default.)
        options = ClaudeAgentOptions(
            model=model,
            cwd=str(cwd),
            permission_mode="dontAsk",
            disallowed_tools=list(_DISALLOWED_TOOLS),
            # Read-anywhere (user-confirmed policy: read any file, write nothing). Under dontAsk
            # the Read tool is denied outside cwd (a permission-layer gate, separate from the write
            # sandbox), so grant the filesystem root as readable — the deliberate read-anywhere
            # contract (N4): reviewers read references kept outside the repo. Writes stay blocked by
            # the sandbox + disallowed tools; dontAsk still denies network. Live-verified.
            add_dirs=["/"],
            output_format={"type": "json_schema", "schema": schema},
            settings=_SANDBOX_SETTINGS,
            extra_args=extra_args,
            cli_path=self._cli_path,
        )
        # Live JSONL event log: tee every SDK message as it arrives, then a terminal result/error
        # line. Write-only diagnostic (the report reads review.json, never this), so a log failure
        # is best-effort inside the writer and can't mask the AdapterError-only failure contract.
        writer = EventLogWriter(log_path, harness=self.name, model=model)
        try:
            result, transcript = await self._consume(prompt, options, timeout, writer)
            out = self._interpret(result, transcript)
            if validate is not None:
                # Validate INSIDE the writer's scope so a schema-invalid review logs a terminal
                # error, not a false success (the SDK's structured_output can be present but
                # off-schema). The generic dedup caller omits this.
                try:
                    validate(out.payload)
                except Exception as exc:  # noqa: BLE001 - any validation failure fails fast
                    raise AdapterError(f"claude output failed schema validation: {exc}") from exc
        except AdapterError as exc:
            writer.error(str(exc))
            raise
        else:
            writer.result()
            return out
        finally:
            writer.close()

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
            prompt,
            review_output_json_schema(),
            model,
            cwd,
            log_path,
            thinking,
            timeout,
            validate=ReviewOutput.model_validate,
        )
        review = ReviewOutput.model_validate(out.payload)
        return HarnessOutput(review=review, usage=out.usage, raw=out.raw)

    async def _consume(
        self,
        prompt: str,
        options: ClaudeAgentOptions,
        timeout: float | None,
        writer: EventLogWriter,
    ) -> tuple[ResultMessage | None, list[str]]:
        """Drive the query generator to completion, capturing the assistant transcript and the
        terminal ResultMessage. `asyncio.timeout(None)` is a no-op, so this bounds the run only
        when a timeout is set. A timeout is fail-fast (non-transient), matching the CLI path."""
        transcript: list[str] = []
        result: ResultMessage | None = None
        # query() is an async generator at runtime (it has aclose); its return annotation widens
        # to AsyncIterator, which lacks aclose, so cast to the real type for the teardown below.
        agen = cast("AsyncGenerator[Message]", query(prompt=prompt, options=options))
        try:
            async with asyncio.timeout(timeout):
                async for message in agen:
                    writer.append(message)  # tee every SDK message to the live JSONL log
                    if isinstance(message, AssistantMessage):
                        transcript.extend(
                            b.text for b in message.content if isinstance(b, TextBlock)
                        )
                    elif isinstance(message, ResultMessage):
                        result = message
        except TimeoutError as exc:
            raise AdapterError(f"claude timed out after {timeout}s", transient=False) from exc
        except CLINotFoundError as exc:
            raise AdapterError(f"claude binary not found: {exc}", transient=False) from exc
        except ProcessError as exc:
            detail = (exc.stderr or str(exc)).strip()
            raise AdapterError(
                f"claude process failed: {detail}",
                transient=classify_transient(exc.exit_code or 1, detail),
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalize EVERY other failure to AdapterError
            # Malformed JSON, SDK/anyio internals, any unexpected error → AdapterError, so the
            # adapter's only failure type is AdapterError. The dedup path (run_dedup) catches only
            # AdapterError/ValidationError, so an unnormalized exception would abort the merge and
            # leave the run stuck non-terminal. Classify transient by text so an overload that
            # surfaces as some non-ProcessError exception still retries rather than failing fast.
            # (CancelledError is a BaseException, not Exception, so cancellation is not swallowed.)
            detail = str(exc)
            raise AdapterError(
                f"claude SDK call failed: {detail}", transient=looks_transient(detail)
            ) from exc
        finally:
            # PEP 533: an `async for` does not close its iterator when the body/await raises, so on
            # timeout/cancel the SDK's transport (the claude subprocess) would leak. aclose() runs
            # the generator's finally → transport disconnect (SIGTERM→SIGKILL). Best-effort: the
            # meaningful error is already chosen above, so a teardown error must not mask it.
            with contextlib.suppress(Exception):
                await agen.aclose()
        return result, transcript

    def _interpret(self, result: ResultMessage | None, transcript: list[str]) -> StructuredOutput:
        if result is None:
            raise AdapterError("claude produced no result message")
        if result.is_error:
            status = result.api_error_status
            text = " ".join(result.errors or []) or (result.result or "") or result.subtype
            transient = status in _TRANSIENT_STATUS or looks_transient(text)
            raise AdapterError(f"claude reported an error: {status or text}", transient=transient)
        if result.structured_output is None:
            raise AdapterError("claude output had no structured_output (schema not honored)")
        return StructuredOutput(
            payload=result.structured_output, usage=self._usage(result), raw="\n".join(transcript)
        )

    def preflight(self) -> Preflight:
        # The SDK resolves a bundled `claude` that need not be on PATH, so don't gate on `which`.
        # Confirm a binary resolves (override → bundled → PATH), then probe auth with it.
        binary = self._resolve_cli(self._cli_path)
        if binary is None:
            return Preflight("fail", "claude binary not resolvable (no override, bundle, or PATH)")
        probe = [binary, *self.auth_status_args]
        if run_sync(probe, timeout=AUTH_PROBE_TIMEOUT).returncode == 0:
            return Preflight("ok", f"claude SDK ready ({binary})")
        return Preflight("warn", f"claude SDK present ({binary}); auth could not be verified")

    def _resolve_cli(self, override: str | None) -> str | None:
        # Mirror the SDK's resolution order for doctor: explicit override → bundled → PATH. which()
        # handles an absolute path OR a bare command name AND verifies it's executable, so a
        # non-executable override resolves to None (doctor fails it) instead of a broken probe.
        if override:
            return which(override)
        # Reach into the SDK's bundled-binary location; if that private layout ever changes,
        # exists() is False and we degrade to PATH rather than breaking.
        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / self.binary
        if bundled.exists():
            return str(bundled)
        return which(self.binary)

    @staticmethod
    def _usage(result: ResultMessage) -> Usage:
        u = result.usage or {}
        return Usage(
            input_tokens=int(u.get("input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cost_usd=float(result.total_cost_usd or 0.0),
        )
