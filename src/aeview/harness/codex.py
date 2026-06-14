"""codex adapter: schema-constrained final message via the OpenAI Codex Python SDK.

Runs Codex through the `openai-codex` SDK (`AsyncCodex().thread_start(...).run(...)`) instead of
shelling out to `codex exec`. The SDK resolves its own bundled `codex` binary by default (the
`openai-codex-cli-bin` dep wheel); a `settings.harnessBinaries["codex"]` entry overrides it via
`CodexConfig.codex_bin`.

Read-only is Codex's native `sandbox=read-only` + `approval_mode=deny_all` (== approval policy
"never"): the sandbox allows reads anywhere on disk (rg / read-only git still work) and blocks all
writes + network; deny_all keeps the headless run from escalating — every blocked action is
returned to the model immediately, never waiting on approval. This is the read-anywhere/
write-nowhere contract, native to codex (no extra grant needed, unlike claude).

Output is constrained by `output_schema` (OpenAI strict mode), so the turn's final message is the
schema-conforming JSON; the SDK does not strictify the schema, so we pass `make_strict_schema`
ourselves and parse `final_response`. Token usage comes from `result.usage.total` (no USD cost).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    CodexConfig,
    CodexError,
    Sandbox,
    is_retryable_error,
)
from openai_codex.types import ReasoningEffort
from pydantic import ValidationError

from ..process import run_sync
from ..schema import ReviewOutput, Usage, make_strict_schema, review_output_json_schema
from .base import (
    AUTH_PROBE_TIMEOUT,
    AdapterError,
    HarnessOutput,
    Preflight,
    SchemaSupport,
    StructuredOutput,
    looks_transient,
)

if TYPE_CHECKING:
    from openai_codex import TurnResult


class CodexAdapter:
    name: str = "codex"
    schema_support: SchemaSupport = "constrained"
    auth_status_args: list[str] = ["login", "status"]  # noqa: RUF012

    def __init__(self, binary_override: str | None = None) -> None:
        # settings.harnessBinaries["codex"], threaded to the SDK via CodexConfig.codex_bin. None
        # (incl. an empty string) → the SDK's bundled codex binary.
        self._codex_bin = binary_override or None
        self.binary = binary_override or "codex"  # protocol attr / doctor display

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
        # Resolve effort before any SDK call so an invalid value fails fast (config error, no log).
        effort = self._resolve_effort(thinking)
        try:
            result = await self._run_isolated(
                self._build_config(),
                prompt,
                make_strict_schema(schema),
                model,
                str(cwd),
                effort,
                timeout,
            )
        except AdapterError as exc:
            # Best-effort log; a log-write failure must not mask the AdapterError or break the
            # adapter's AdapterError-only failure contract.
            with contextlib.suppress(OSError):
                log_path.write_text(f"--- error ---\n{exc}", encoding="utf-8")
            raise
        self._write_log(log_path, result)
        return self._interpret(result)

    async def _run_isolated(
        self,
        config: CodexConfig,
        prompt: str,
        schema: dict,
        model: str,
        cwd: str,
        effort: ReasoningEffort | None,
        timeout: float | None,
    ) -> TurnResult:
        # The SDK multiplexes its blocking RPC (incl. close()) on the shared asyncio.to_thread
        # executor; under aeview's unbounded fan-out a saturated pool could starve a timed-out
        # review's close() and deadlock the teardown. Run each review's SDK interaction in its OWN
        # event loop (on a worker thread) so it has a private executor: close() always runs, and an
        # outer cancellation can't abort it. Concurrent codex reviews are bounded by the process
        # thread pool and serialize beyond it; the inner loop is bounded by the per-review timeout.
        return await asyncio.to_thread(
            asyncio.run, self._consume(config, prompt, schema, model, cwd, effort, timeout)
        )

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

    async def _consume(
        self,
        config: CodexConfig,
        prompt: str,
        schema: dict,
        model: str,
        cwd: str,
        effort: ReasoningEffort | None,
        timeout: float | None,
    ) -> TurnResult:
        """Run one read-only turn to completion inside the caller's (isolated) event loop.
        `asyncio.timeout(None)` is a no-op, so this bounds the run only when a timeout is set; a
        timeout is fail-fast (non-transient), matching every other adapter. The finally ALWAYS
        closes the client: the SDK blocks the actual RPC on a worker thread, so the codex subprocess
        (and that thread) leak until close() terminates it (terminate → 2s → kill, bounded). The
        error chosen above already wins, so a teardown error must not mask it.
        """
        codex = AsyncCodex(config)
        try:
            async with asyncio.timeout(timeout):
                thread = await codex.thread_start(
                    sandbox=Sandbox.read_only,
                    approval_mode=ApprovalMode.deny_all,
                    ephemeral=True,
                    model=model,
                    cwd=cwd,
                )
                return await thread.run(prompt, output_schema=schema, effort=effort)
        except TimeoutError as exc:
            raise AdapterError(f"codex timed out after {timeout}s", transient=False) from exc
        except FileNotFoundError as exc:
            # A bad codex_bin override (the SDK's resolve raises FileNotFoundError) fails fast.
            raise AdapterError(f"codex binary not found: {exc}", transient=False) from exc
        except CodexError as exc:
            # SDK transport/RPC errors; ServerBusyError / overload JSON-RPC is the retryable subset.
            raise AdapterError(
                f"codex SDK error: {exc}", transient=is_retryable_error(exc)
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalize EVERY other failure to AdapterError
            # A failed turn surfaces as RuntimeError(turn.error.message); any other unexpected error
            # also routes here, so the adapter's only failure type is AdapterError (the dedup path
            # catches only AdapterError/ValidationError — an escape would strand the run
            # non-terminal). Classify transient by text so an overload still retries.
            # (CancelledError is a BaseException, not Exception, so cancellation is not swallowed.)
            detail = str(exc)
            raise AdapterError(
                f"codex run failed: {detail}", transient=looks_transient(detail)
            ) from exc
        finally:
            with contextlib.suppress(Exception):
                await codex.close()

    def _interpret(self, result: TurnResult) -> StructuredOutput:
        # .run() already raised RuntimeError on a failed turn, so a returned result completed; an
        # empty final message (None / commentary-only) is still a hard failure for a review.
        final = (result.final_response or "").strip()
        if not final:
            raise AdapterError("codex produced no final message")
        try:
            payload = json.loads(final)
        except json.JSONDecodeError as exc:
            # output_schema constrains decoding, so a non-JSON final message is a hard failure.
            raise AdapterError(f"codex final message was not JSON: {exc}") from exc
        return StructuredOutput(payload=payload, usage=self._usage(result), raw=final)

    def preflight(self) -> Preflight:
        # The SDK resolves a bundled `codex` not necessarily on PATH, so don't gate on PATH. Resolve
        # exactly as the run path does (override → which; else bundled-only) so doctor can't report
        # OK while the run would fail, then probe auth with it.
        binary = self._resolve_codex_bin(self._codex_bin)
        if binary is None:
            return Preflight("fail", "codex binary not resolvable (bad override or no bundle)")
        probe = [binary, *self.auth_status_args]
        if run_sync(probe, timeout=AUTH_PROBE_TIMEOUT).returncode == 0:
            return Preflight("ok", f"codex SDK ready ({binary})")
        return Preflight("warn", f"codex SDK present ({binary}); auth could not be verified")

    def _build_config(self) -> CodexConfig:
        # No override → the SDK resolves its bundled codex binary. With an override, resolve a bare
        # command name or path via which (so "codex" on PATH works); pass it through even when which
        # can't resolve it so the SDK raises FileNotFoundError (fail loud) rather than silently
        # falling back to the bundled binary.
        if self._codex_bin is None:
            return CodexConfig()
        return CodexConfig(codex_bin=which(self._codex_bin) or self._codex_bin)

    def _resolve_codex_bin(self, override: str | None) -> str | None:
        # Match the SDK's ACTUAL resolution so doctor can't report OK while the run would fail: an
        # override resolves via which (abs path or bare name, executability-checked); with NO
        # override the SDK uses ONLY its bundled binary (no PATH fallback), so neither do we.
        if override:
            return which(override)
        try:
            from codex_cli_bin import bundled_codex_path

            bundled = bundled_codex_path()
        except Exception:  # noqa: BLE001 - bundle missing/renamed → unresolved (doctor fails)
            return None
        return str(bundled) if Path(bundled).exists() else None

    def _resolve_effort(self, thinking: str | None) -> ReasoningEffort | None:
        # thinking maps to codex's reasoning effort; "default"/None leaves it unset. Validate
        # against the SDK enum so the accepted set tracks the SDK rather than a hardcoded copy.
        if not thinking or thinking == "default":
            return None
        try:
            return ReasoningEffort(thinking)
        except ValueError as exc:
            valid = [e.value for e in ReasoningEffort]
            raise AdapterError(f"codex thinking '{thinking}' invalid; use one of {valid}") from exc

    def _write_log(self, log_path: Path, result: TurnResult) -> None:
        usage = self._usage(result)
        summary = {
            "status": result.status.value if result.status is not None else None,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }
        lines = [result.final_response or "", "--- result ---", json.dumps(summary, default=str)]
        # Best-effort: a failed log write must not break the AdapterError-only failure contract.
        with contextlib.suppress(OSError):
            log_path.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _usage(result: TurnResult) -> Usage:
        # codex reports no USD cost; usage is optional (the tokenUsage event may not arrive).
        if result.usage is None:
            return Usage(cost_usd=0.0)
        total = result.usage.total
        return Usage(
            input_tokens=total.input_tokens, output_tokens=total.output_tokens, cost_usd=0.0
        )
