"""copilot adapter: prompt-embedded schema, deny-by-default read-only, via the Copilot SDK.

Runs GitHub Copilot through the `github-copilot-sdk` (`CopilotClient().create_session(...)` +
`session.send_and_wait(...)`) instead of shelling out to the `copilot` CLI. The SDK resolves its
own bundled `copilot` binary by default (shipped in the platform wheel); a
`settings.harnessBinaries["copilot"]` entry overrides it via `RuntimeConnection.for_stdio`.

Copilot has no schema flag (`schema_support="prompt"`): the wanted JSON Schema is appended to the
prompt with a strict "return ONLY this JSON" instruction, and aeview validates + re-prompts once on
invalid output — the one place `schema_support` drives the reaction. The model's answer is the final
`AssistantMessageData.content` (a plain string) returned by `send_and_wait`; usage arrives as
`AssistantUsageData` events (input + output tokens, no USD cost) captured via `session.on`.

Read-only is enforced entirely by the `on_permission_request` callback, deny-by-default: it approves
ONLY file reads (any path) and denies every other kind (write/shell/url/mcp/...). Copilot has no
native read-only sandbox and HANGS headless without a handler, so this is the boundary — a new
mutating tool-kind is blocked automatically, while new read tools auto-enable. (Native sandbox is
coming to Copilot — migrate to it when it ships.)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import os
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, cast, get_args

from copilot import CopilotClient, RuntimeConnection
from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
from copilot.session import ReasoningEffort
from copilot.session_events import AssistantMessageData, AssistantUsageData

from ..schema import ReviewOutput, Usage, review_output_json_schema
from .base import (
    AdapterError,
    HarnessOutput,
    Preflight,
    SchemaSupport,
    StructuredOutput,
    looks_transient,
)
from .eventlog import EventLogWriter

if TYPE_CHECKING:
    from copilot import CopilotSession, PermissionRequest, PermissionRequestResult, SessionEvent

# copilot reasoning-effort levels, derived from the SDK's ReasoningEffort Literal so the accepted
# set tracks the SDK; "default"/None leaves it unset. (Narrower than the old CLI set — no none/max.)
_EFFORT_LEVELS = frozenset(get_args(ReasoningEffort))

# Prompt-only can't guarantee conformance, so re-prompt once on invalid output, then fail.
_MAX_ATTEMPTS = 2

# send_and_wait requires a float timeout (defaults to 60s, which would fire prematurely). When no
# per-review timeout is set, pass an effectively-unbounded value and let the caller's bound (none)
# govern — in practice reviewTimeoutSeconds is always set, so this only covers the None path.
_UNBOUNDED_TIMEOUT = 365 * 24 * 60 * 60

# Cap on the client teardown: stop() awaits a destroy RPC (which can hang on a dead connection)
# before its own bounded terminate→kill, so we bound stop() and fall back to the SDK's force_stop().
_TEARDOWN_GRACE = 10.0

# The retry turn rides the SAME session, so the model still sees its first (bad) answer + the schema
# above it — this just re-states the format requirement more forcefully.
_RETRY_SUFFIX = (
    "Respond with ONLY the JSON object described above — no prose, no explanation, no markdown "
    "fence, nothing before or after the object."
)


class CopilotAdapter:
    name: str = "copilot"
    schema_support: SchemaSupport = "prompt"
    auth_status_args: list[str] = []  # no no-cost auth probe; preflight warns  # noqa: RUF012

    def __init__(self, binary_override: str | None = None) -> None:
        # settings.harnessBinaries["copilot"], threaded to the SDK via RuntimeConnection.for_stdio.
        # None (incl. an empty string) → the SDK's bundled copilot binary.
        self._copilot_bin = binary_override or None
        self.binary = binary_override or "copilot"  # protocol attr / doctor display

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
        """`validate` (optional) is a deep schema check (e.g. ReviewOutput.model_validate) used by
        the review path (`run`): it raises on a structurally-present-but-invalid payload (wrong
        enum/type) so that case re-prompts too. The generic dedup caller omits it and one-shots — a
        deep-invalid dedup payload degrades to the raw-union path."""
        # Resolve effort before any SDK call so an invalid value fails fast (config error, no log).
        effort = self._resolve_effort(thinking)
        base_prompt = _embed_schema(prompt, schema)
        return await self._run_isolated(
            self._build_connection(),
            base_prompt,
            schema,
            model,
            str(cwd),
            effort,
            timeout,
            validate,
            log_path,
        )

    async def _run_isolated(
        self,
        connection: RuntimeConnection | None,
        base_prompt: str,
        schema: dict,
        model: str,
        cwd: str,
        effort: ReasoningEffort | None,
        timeout: float | None,
        validate: Callable[[dict], object] | None,
        log_path: Path,
    ) -> StructuredOutput:
        # The SDK runs its JSON-RPC writes on the loop's default executor and teardown (stop()'s
        # session.destroy) awaits an RPC too; under aeview's unbounded fan-out a saturated shared
        # pool could starve a timed-out review's teardown → deadlock. So run each review's SDK work
        # in its OWN event loop on a DEDICATED daemon thread: a private executor keeps teardown from
        # being starved; one thread per review (unbounded, like the fan-out) avoids a shared
        # cap/queue wait; and as a daemon it can't wedge interpreter exit on Ctrl-C (an abandoned
        # read-only turn finishes on its own; a clean subtree-kill is deferred I6b-2 work). Mirrors
        # the codex adapter's teardown isolation (same blocking-I/O-on-the-shared-executor shape).
        outcome: concurrent.futures.Future[StructuredOutput] = concurrent.futures.Future()

        def _runner() -> None:
            # Claim the future before running so a wrap_future cancel() from the awaiting loop can't
            # race set_result into InvalidStateError; if already cancelled, skip the run.
            if not outcome.set_running_or_notify_cancel():
                return
            coro = self._consume(
                connection, base_prompt, schema, model, cwd, effort, timeout, validate, log_path
            )
            try:
                outcome.set_result(asyncio.run(coro))
            except BaseException as exc:  # noqa: BLE001 - marshal every outcome to the awaiting loop
                outcome.set_exception(exc)

        try:
            threading.Thread(target=_runner, name="aeview-copilot-turn", daemon=True).start()
        except RuntimeError as exc:
            # OS thread-limit exhaustion under heavy fan-out: normalize to a transient AdapterError
            # (the adapter's only failure type; a retry may succeed as sibling reviews finish) so a
            # raw RuntimeError can't break the dedup path's AdapterError-only contract.
            raise AdapterError(
                f"copilot worker thread failed to start: {exc}", transient=True
            ) from exc
        return await asyncio.wrap_future(outcome)

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
        connection: RuntimeConnection | None,
        base_prompt: str,
        schema: dict,
        model: str,
        cwd: str,
        effort: ReasoningEffort | None,
        timeout: float | None,
        validate: Callable[[dict], object] | None,
        log_path: Path,
    ) -> StructuredOutput:
        """Run one read-only review (with one re-prompt on invalid output) to completion inside the
        caller's (isolated) event loop. `asyncio.timeout(None)` is a no-op, so this bounds the run
        only when a timeout is set; a timeout is fail-fast (non-transient), matching every other
        adapter. ALL teardown is `_teardown_client` in the outer finally — OUTSIDE the review
        timeout and bounded — so a slow/hung teardown can neither consume the review timeout nor
        mask a valid parsed result (we don't disconnect the session explicitly: client.stop()
        destroys it, and force_stop() is the hard-kill fallback). On the daemon thread a wedged
        teardown can't block aeview; the subprocess finishes (subtree-kill is deferred I6b-2)."""
        collector = _UsageCollector()
        writer = EventLogWriter(log_path, harness=self.name, model=model)
        client: CopilotClient | None = None
        session_id: str | None = None
        try:
            async with asyncio.timeout(timeout):
                # Construct inside the try so a "Copilot CLI not found" RuntimeError (raised in
                # CopilotClient.__init__) is normalized to AdapterError like any other failure.
                client = CopilotClient(working_directory=cwd, connection=connection)
                await client.start()
                # Minimal session by design: read-only is enforced SOLELY by on_permission_request
                # (deny-by-default, approve only kind="read"). Every mutating/exec/network action
                # routes through that callback (the headless runtime hangs without one), so leaving
                # the SDK's feature flags (host git ops, skills, MCP, file hooks, config discovery —
                # all None/off here) at their defaults can't bypass it. We deliberately do NOT add a
                # second tool-gating layer (the user chose handler-only); live-proven write-nowhere.
                # Pre-generate the id and pass it to create_session so the finally can delete this
                # session on EVERY path the runtime may have allocated it — including a create that
                # times out or errors AFTER allocating the dir but before returning the session.
                # (stop() preserves the dir; aeview never resumes.) Deleting a never-allocated id is
                # a harmless suppressed no-op.
                session_id = str(uuid.uuid4())
                session = await client.create_session(
                    session_id=session_id,
                    model=model or None,
                    reasoning_effort=effort,
                    on_permission_request=_deny_by_default_permission,
                    working_directory=cwd,
                )

                # One subscription tees every SessionEvent to the live log AND feeds the usage
                # collector — session.on already delivers the full event stream (we previously kept
                # only usage from it).
                def _on_event(event: SessionEvent) -> None:
                    writer.append(event)
                    collector.handle(event)

                unsubscribe = session.on(_on_event)
                try:
                    payload, raw = await _run_turns(session, base_prompt, schema, validate)
                finally:
                    unsubscribe()
                writer.result()
                # Built + returned here so payload/raw are in scope; the finally's bounded teardown
                # runs on the way out and can't mask this result (it never raises).
                return StructuredOutput(
                    payload=payload,
                    usage=Usage(
                        input_tokens=collector.input_tokens,
                        output_tokens=collector.output_tokens,
                        cost_usd=0.0,  # see _UsageCollector: the SDK's `cost` is ignored
                    ),
                    raw=raw,
                )
        except AdapterError as exc:
            # Already classified (e.g. invalid-after-retries) — don't re-wrap/re-classify.
            writer.error(str(exc))
            raise
        except TimeoutError as exc:
            msg = f"copilot timed out after {timeout}s"
            writer.error(msg)
            raise AdapterError(msg, transient=False) from exc
        except Exception as exc:  # noqa: BLE001 - normalize EVERY other failure to AdapterError
            # The SDK has no public base error or retryable helper, so classify transient by text
            # (rate-limit/overload still retries), matching the old CLI path. The dedup path catches
            # only AdapterError, so an escape strands the run non-terminal. (CancelledError is a
            # BaseException, not Exception — cancellation isn't caught.)
            detail = str(exc)
            msg = f"copilot run failed: {detail}"
            writer.error(msg)
            raise AdapterError(msg, transient=looks_transient(detail)) from exc
        finally:
            writer.close()
            if client is not None:
                await _teardown_client(client, session_id)

    def preflight(self) -> Preflight:
        # The SDK resolves a bundled `copilot` not necessarily on PATH, so don't gate on PATH.
        # Resolve exactly as the run path does (override → which; else bundled-only) so doctor can't
        # report OK while the run would fail. Copilot has no no-cost auth probe → we can only warn.
        binary = self._resolve_copilot_bin()
        if binary is None:
            return Preflight("fail", "copilot binary not resolvable (bad override or no bundle)")
        return Preflight("warn", f"copilot SDK ready ({binary}); auth not verifiable")

    def _build_connection(self) -> RuntimeConnection | None:
        # No override → SDK resolves its bundled copilot binary. With an override, resolve it the
        # same way preflight predicts (_resolve_via_sdk_rule: exists-and-executable, else PATH);
        # pass the raw override through when that fails so the SDK fails loud (spawn error) rather
        # than silently falling back to the bundled binary.
        if self._copilot_bin is None:
            return None
        resolved = _resolve_via_sdk_rule(self._copilot_bin) or self._copilot_bin
        return RuntimeConnection.for_stdio(path=resolved)

    def _resolve_copilot_bin(self) -> str | None:
        # Predict the SDK's resolution so doctor can't report OK while the run would fail. The SDK's
        # ORDER is: aeview override (our connection.path) → COPILOT_CLI_PATH env → bundled binary;
        # it then resolves whichever wins with `os.path.exists(p) or shutil.which(p)` at spawn (so a
        # bare command on PATH works), and FAILS if neither hits. We mirror that per-candidate rule
        # via `_resolve_via_sdk_rule`. There is no `which("copilot")` fallback for an unconfigured
        # bare binary, so an empty (no override / no env / no bundle) case stays unresolved → fail.
        if self._copilot_bin:
            return _resolve_via_sdk_rule(self._copilot_bin)
        env_path = os.environ.get("COPILOT_CLI_PATH")
        if env_path:
            return _resolve_via_sdk_rule(env_path)
        try:
            from copilot.client import _get_bundled_cli_path

            bundled = _get_bundled_cli_path()
        except Exception:  # noqa: BLE001 - bundle missing/renamed → unresolved (doctor fails)
            return None
        return _resolve_via_sdk_rule(bundled) if bundled else None

    def _resolve_effort(self, thinking: str | None) -> ReasoningEffort | None:
        # thinking maps to copilot's reasoning effort; "default"/None leaves it unset. Validate
        # against the SDK's level set so the accepted values track the SDK.
        if not thinking or thinking == "default":
            return None
        if thinking not in _EFFORT_LEVELS:
            raise AdapterError(
                f"copilot thinking '{thinking}' invalid; use one of {sorted(_EFFORT_LEVELS)}"
            )
        return cast(ReasoningEffort, thinking)


class _UsageCollector:
    """Accumulates copilot token usage across a turn's assistant.usage events (there can be several
    per turn — one per model call, and the re-prompt adds more — so accumulate; send_and_wait does
    not expose usage in its return, so we subscribe via session.on).

    AssistantUsageData also carries an EXPERIMENTAL `cost` field; we intentionally ignore it and
    report cost_usd=0.0 (matching codex). USD cost accounting is claude-only (its SDK reports a
    stable total_cost_usd); copilot's `cost` is experimental/unreliable, so folding it in would make
    the per-harness cost inconsistent. Revisit if/when the SDK marks `cost` stable."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    def handle(self, event: SessionEvent) -> None:
        data = event.data
        if isinstance(data, AssistantUsageData):
            self.input_tokens += data.input_tokens or 0
            self.output_tokens += data.output_tokens or 0


def _deny_by_default_permission(
    request: PermissionRequest, _invocation: dict[str, str]
) -> PermissionRequestResult:
    # Read-anywhere / write-nowhere, deny-by-default: approve ONLY file reads (any path — fencing is
    # deferred to the operator/MDM, per the project's read-scope decision); deny every other kind
    # (write/shell/url/mcp/memory/hook/...). copilot has no native read-only sandbox and HANGS
    # headless without a handler, so this is THE enforcement boundary; deny-by-default means a new
    # mutating tool-kind is blocked automatically. (Native sandbox is coming — migrate to it then.)
    if request.kind == "read":
        return PermissionDecisionApproveOnce()
    return PermissionDecisionReject()


def _resolve_via_sdk_rule(path: str) -> str | None:
    # Predict the SDK's spawn OUTCOME (client.py uses the path verbatim if it exists, else
    # shutil.which(path), then Popen). We require an existing path to be an executable FILE (a dir
    # or non-exec file would resolve for the SDK but fail at Popen, so doctor must reject it too),
    # and fall back to which() (which itself enforces executability) for a bare command on PATH.
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return which(path)


async def _teardown_client(client: CopilotClient, session_id: str | None) -> None:
    # Best-effort, bounded teardown. First delete THIS review's on-disk session: stop() only
    # disconnects (the SDK keeps session-state/<id> for resume), and aeview never resumes — so
    # without this each review leaks a ~/.copilot session dir. delete_session needs the live
    # connection, so it MUST run before stop(); it's bounded + suppressed like stop() (a hung or
    # failed delete can't mask the result or block, and stop() still runs after). Only our own
    # session_id is deleted, never the user's other ~/.copilot sessions.
    if session_id is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.delete_session(session_id), timeout=_TEARDOWN_GRACE)
    # stop() disconnects sessions + awaits a destroy RPC (which can hang on a dead connection)
    # before its own bounded terminate→kill, so cap it and fall back to force_stop() (the SDK's
    # escape for a stuck stop()). A teardown that raises or hangs must neither mask a valid parsed
    # result nor block — on the daemon thread a wedged teardown only abandons the subprocess (it
    # finishes on its own; clean subtree-kill is deferred I6b-2).
    try:
        await asyncio.wait_for(client.stop(), timeout=_TEARDOWN_GRACE)
    except Exception:  # noqa: BLE001 - bounded; fall back to the hard kill, never propagate
        # force_stop() can also hang on a stuck transport, so bound it too (and suppress errors).
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.force_stop(), timeout=_TEARDOWN_GRACE)


async def _run_turns(
    session: CopilotSession,
    base_prompt: str,
    schema: dict,
    validate: Callable[[dict], object] | None,
) -> tuple[dict, str]:
    """Send the prompt and parse the schema-conforming object out of copilot's answer; on invalid
    output re-prompt once on the SAME session (the model still sees its first answer + the schema),
    then fail. Returns (payload, raw-answer-text)."""
    last_error = "copilot produced no valid output"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        text = base_prompt if attempt == 1 else _RETRY_SUFFIX
        # _UNBOUNDED only defeats send_and_wait's 60s default; the per-review bound is _consume's
        # outer asyncio.timeout (which spans both turns), so a long review isn't cut off mid-turn.
        event = await session.send_and_wait(text, timeout=_UNBOUNDED_TIMEOUT)
        answer = _final_text(event)
        # _extract_json returns None for an empty answer too (no `{` to scan), so no answer, a
        # JSON-less answer, and an unparseable answer all funnel to the same re-prompt path.
        payload = _extract_json(answer, schema)
        if payload is None:
            last_error = "copilot did not return a JSON object matching the schema"
            continue
        if validate is not None:
            try:
                validate(payload)
            except Exception as exc:  # noqa: BLE001 - any validation failure should re-prompt
                last_error = f"copilot output failed schema validation: {exc}"
                continue
        return payload, answer
    raise AdapterError(last_error)


def _final_text(event: SessionEvent | None) -> str:
    # send_and_wait returns the last assistant.message event (or None on no answer); the model's
    # text is its AssistantMessageData.content (a plain string).
    if event is None:
        return ""
    data = event.data
    if isinstance(data, AssistantMessageData) and isinstance(data.content, str):
        return data.content
    return ""


def _embed_schema(prompt: str, schema: dict) -> str:
    return (
        f"{prompt}\n\n"
        f"## Required output format\n\n"
        f"Respond with a single JSON object conforming exactly to this JSON Schema. Output ONLY "
        f"that JSON object — no prose, no explanation, no markdown fence.\n\n"
        f"```json\n{json.dumps(schema)}\n```\n"
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


def _extract_json(answer: str, schema: dict) -> dict | None:
    """Pull the schema-conforming object out of copilot's answer text.

    Scan each `{` with json.raw_decode — the real parser, so it handles strings/escapes/nesting
    correctly and ignores trailing prose/fences — and keep the first object that matches the schema.
    A prompt-only model may wrap the answer (e.g. {"output": {...}}); keep the first nested match as
    a fallback. We retain only one candidate, so a many-object response can't spike memory.
    """
    required = set(schema.get("required", []))
    properties = set(schema.get("properties", {}))
    nested_fallback: dict | None = None
    for obj in _json_objects(answer):
        if _matches(obj, required, properties):
            return obj
        if nested_fallback is None:
            nested_fallback = _find_nested_match(obj, required, properties)
    return nested_fallback


def _find_nested_match(value: object, required: set[str], properties: set[str]) -> dict | None:
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
            if isinstance(child, dict) and _matches(child, required, properties):
                return child
            if isinstance(child, (dict, list)):
                queue.append(child)
    return None


def _json_objects(text: str) -> Iterator[dict]:
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


def _matches(obj: object, required: set[str], properties: set[str]) -> bool:
    if not isinstance(obj, dict) or not required <= obj.keys():
        return False
    # An all-defaulted schema (no required keys, e.g. DuplicateGroups) would otherwise accept a
    # stray `{}` before the real answer. Demand at least one of the schema's own properties — not
    # all of them, so a payload that legitimately omits optional fields still matches.
    if not required and properties:
        return bool(obj.keys() & properties)
    return True
