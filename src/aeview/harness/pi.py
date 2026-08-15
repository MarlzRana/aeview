"""pi adapter: prompt-embedded schema + SRT sandbox via the pi CLI.

Runs pi through its print-JSON CLI (`pi -p --mode json`), wrapped in Anthropic Sandbox Runtime
(`srt`) so the whole process tree is read-anywhere / write-nowhere except this review's own
`pi-session/` dir. There is no Python SDK and no `--output-schema`; schema_support is "prompt"
(embed + extract, re-prompt once on the persisted session — Copilot's policy).

pi and srt are PATH-gated (not bundled). `settings.overrideHarnessBinaries["pi"]` is argv[0] for
pi; srt is always `which("srt")`. Network is an SRT allowlist: built-in provider hosts, overridable
via `harnessSettings.pi` (`onlySandboxAllowedDomains` replaces; otherwise
`extraSandboxAllowedDomains` unions onto the built-in list).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from shutil import which

from ..config import PiHarnessSettings, load_settings
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
from .prompt_schema import MAX_ATTEMPTS, RETRY_SUFFIX, embed_schema, extract_json

# pi thinking levels from its CLI (`--thinking`); "default"/None leaves the flag unset.
_THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})

# Common SaaS hosts pi's model HTTP hits. Custom / Azure / Bedrock / LAN Ollama go in
# harnessSettings.pi.extraSandboxAllowedDomains (or a full replacement via only…).
BUILTIN_ALLOWED_DOMAINS: tuple[str, ...] = (
    "api.anthropic.com",
    "*.anthropic.com",
    "api.openai.com",
    "*.openai.com",
    "generativelanguage.googleapis.com",
    "*.googleapis.com",
    "openrouter.ai",
    "*.openrouter.ai",
    "api.x.ai",
    "*.x.ai",
    "api.githubcopilot.com",
    "api.github.com",
    "localhost",
)

_TOOLS = "read,grep,find,ls,bash"
_SESSION_ID = "aeview"
# Replaces pi's default coding-assistant system prompt so it doesn't fight the reviewer.
_SYSTEM_PROMPT = (
    "You are performing a structured code review. Follow the user prompt exactly. "
    "Your only output is the JSON object required by the prompt."
)
_SRT_BIN = "srt"


def resolve_allowed_domains(settings: PiHarnessSettings) -> list[str]:
    """`only` replaces the built-in list (and extra is ignored); otherwise extra unions on."""
    if settings.only_sandbox_allowed_domains is not None:
        return list(settings.only_sandbox_allowed_domains)
    extra = settings.extra_sandbox_allowed_domains
    # Preserve built-in order; append extras that aren't already present.
    seen = set(BUILTIN_ALLOWED_DOMAINS)
    out = list(BUILTIN_ALLOWED_DOMAINS)
    for host in extra:
        if host not in seen:
            seen.add(host)
            out.append(host)
    return out


def review_dirs(log_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    """Derive this review's artifact paths from the log path.

    Fan-out / dedup pass `review.log` or `dedup.log` under the instance dir; everything pi
    writes is a sibling so concurrent reviews (distinct instance dirs, distinct run-ids) cannot
    collide. The wipe at run_structured start only touches *this* review's pi dirs.
    """
    review_dir = log_path.parent
    return (
        review_dir,
        review_dir / "pi-session",
        review_dir / "pi-agent",
        review_dir / "pi-tmp",
        review_dir / "srt-settings.json",
    )


# Files copied from the user's ~/.pi/agent into the per-review agent dir so pi can authenticate
# and resolve models without writing lock files back into the real home (SRT would EPERM those).
_SEEDED_AGENT_FILES = ("auth.json", "models.json", "settings.json")


def _seed_agent_dir(agent_dir: Path) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    src_root = Path.home() / ".pi" / "agent"
    for name in _SEEDED_AGENT_FILES:
        src = src_root / name
        if src.is_file():
            shutil.copy2(src, agent_dir / name)
    # Drop package sources so the isolated agent does not try to `npm install`
    # into the sandbox (registry is not on the allowlist; we also pass
    # --no-extensions). Auth/models stay; packages do not.
    settings_path = agent_dir / "settings.json"
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        if isinstance(data, dict) and data.pop("packages", None) is not None:
            settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _scrub_seeded_secrets(agent_dir: Path) -> None:
    # auth.json / models.json were copied so pi could authenticate inside the sandbox.
    # They must not persist in the run tree (retention keeps runs for days; a tarball of
    # ~/.aeview/runs would leak keys). Settings stay — no secrets there after packages drop.
    for name in ("auth.json", "models.json"):
        path = agent_dir / name
        path.unlink(missing_ok=True)


class PiAdapter:
    name: str = "pi"
    schema_support: SchemaSupport = "prompt"
    auth_status_args: list[str] = []  # probe needs --model; preflight warns  # noqa: RUF012

    def __init__(self, binary_override: str | None = None) -> None:
        # settings.overrideHarnessBinaries["pi"]. None / empty → "pi" on PATH.
        self.binary = binary_override or "pi"

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
        """`validate` (optional) is a deep schema check used by `run` so a structurally-present-
        but-invalid payload re-prompts too. The generic dedup caller omits it and one-shots."""
        thinking_flag = self._resolve_thinking(thinking)
        pi_bin = self._resolve_pi()
        srt_bin = self._resolve_srt()
        _, session_dir, agent_dir, tmp_dir, settings_path = review_dirs(log_path)
        # Cold start: wipe only THIS review's pi dirs so a previous failed attempt (or an
        # aeview resume) doesn't leak conversation. The in-method schema re-prompt reuses the
        # just-created session. Sibling reviews / other run-ids are different parents.
        for d in (session_dir, agent_dir, tmp_dir):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
        _seed_agent_dir(agent_dir)
        self._write_srt_settings(settings_path, session_dir, agent_dir, tmp_dir)

        base_prompt = embed_schema(prompt, schema)
        writer = EventLogWriter(log_path, harness=self.name, model=model)
        try:
            # One budget for both attempts (schema re-prompt included), matching Copilot.
            async with asyncio.timeout(timeout):
                out = await self._run_attempts(
                    pi_bin,
                    srt_bin,
                    settings_path,
                    session_dir,
                    agent_dir,
                    tmp_dir,
                    base_prompt,
                    schema,
                    model,
                    cwd,
                    thinking_flag,
                    validate,
                    writer,
                )
        except TimeoutError as exc:
            msg = f"pi timed out after {timeout}s"
            writer.error(msg)
            raise AdapterError(msg, transient=False) from exc
        except AdapterError as exc:
            writer.error(str(exc))
            raise
        else:
            writer.result()
            return out
        finally:
            _scrub_seeded_secrets(agent_dir)
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

    async def _run_attempts(
        self,
        pi_bin: str,
        srt_bin: str,
        settings_path: Path,
        session_dir: Path,
        agent_dir: Path,
        tmp_dir: Path,
        base_prompt: str,
        schema: dict,
        model: str,
        cwd: Path,
        thinking: str | None,
        validate: Callable[[dict], object] | None,
        writer: EventLogWriter,
    ) -> StructuredOutput:
        last_error = "pi produced no valid output"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            text = base_prompt if attempt == 1 else RETRY_SUFFIX
            argv = self._argv(
                srt_bin, settings_path, pi_bin, session_dir, tmp_dir, model, thinking
            )
            answer, usage = await self._invoke(argv, text, cwd, agent_dir, tmp_dir, writer)
            raw = answer
            parsed = extract_json(answer, schema)
            if parsed is None:
                last_error = "pi did not return a JSON object matching the schema"
                continue
            if validate is not None:
                try:
                    validate(parsed)
                except Exception as exc:  # noqa: BLE001 - any validation failure should re-prompt
                    last_error = f"pi output failed schema validation: {exc}"
                    continue
            return StructuredOutput(payload=parsed, usage=usage, raw=raw)
        raise AdapterError(last_error)

    async def _invoke(
        self,
        argv: list[str],
        prompt: str,
        cwd: Path,
        agent_dir: Path,
        tmp_dir: Path,
        writer: EventLogWriter,
    ) -> tuple[str, Usage]:
        """Spawn srt→pi, stream JSONL stdout into the event log, return (answer-text, usage)."""
        env = os.environ.copy()
        # Isolate pi's config/auth/locks inside this review so SRT never has to allow writes
        # to the real ~/.pi/agent. Do NOT set TMPDIR here: srt inherits this env and its mux
        # Unix socket EINVAL's under a deep path (sandbox-exec). Reviewer scratch is set on
        # the inner `env TMPDIR=pi-tmp pi …` argv instead.
        env["PI_CODING_AGENT_DIR"] = str(agent_dir)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AdapterError(f"pi binary not found: {exc}", transient=False) from exc
        except OSError as exc:
            raise AdapterError(f"pi failed to start: {exc}", transient=False) from exc

        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        try:
            # Feed stdin concurrently with the readers. An inline review prompt can exceed the
            # pipe buffer (~64KB); if we drain first, a child that already wrote session-start
            # JSONL deadlocks until the outer timeout. communicate() does the same gather.
            events, stderr, _ = await asyncio.gather(
                _read_jsonl(proc.stdout, writer),
                proc.stderr.read(),
                _feed_stdin(proc.stdin, prompt),
            )
            returncode = await proc.wait()
        except TimeoutError:
            await _kill(proc)
            raise
        except AdapterError:
            await _kill(proc)
            raise
        except Exception as exc:  # noqa: BLE001 - normalize EVERY other failure to AdapterError
            await _kill(proc)
            detail = str(exc)
            raise AdapterError(
                f"pi run failed: {detail}", transient=looks_transient(detail)
            ) from exc

        stderr_text = stderr.decode("utf-8", errors="replace")
        if returncode != 0:
            detail = stderr_text.strip() or f"exit {returncode}"
            raise AdapterError(
                f"pi exited {returncode}: {detail}",
                transient=classify_transient(returncode, detail),
            )
        # A successful exit can still carry a stream-level error (stopReason=error) — surface it.
        stream_error = _stream_error(events)
        if stream_error is not None:
            raise AdapterError(
                f"pi reported an error: {stream_error}",
                transient=looks_transient(stream_error),
            )
        return _last_assistant_text(events), _usage_from_events(events)

    def _argv(
        self,
        srt_bin: str,
        settings_path: Path,
        pi_bin: str,
        session_dir: Path,
        tmp_dir: Path,
        model: str,
        thinking: str | None,
    ) -> list[str]:
        # `--` so srt treats the rest as the command (not its own flags). `--session-dir` +
        # `--session-id` create-or-resume a file under THIS review's dir — never ~/.pi/agent.
        # `env TMPDIR=…` applies only to pi (and its tools), not to srt's mux socket.
        argv = [
            srt_bin,
            "--settings",
            str(settings_path),
            "--",
            "env",
            f"TMPDIR={tmp_dir}",
            f"TMP={tmp_dir}",
            f"TEMP={tmp_dir}",
            pi_bin,
            "-p",
            "--mode",
            "json",
            "--session-dir",
            str(session_dir),
            "--session-id",
            _SESSION_ID,
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--no-approve",
            "--tools",
            _TOOLS,
            "--model",
            model,
            "--system-prompt",
            _SYSTEM_PROMPT,
        ]
        if thinking is not None:
            argv.extend(["--thinking", thinking])
        return argv

    def _write_srt_settings(
        self, path: Path, session_dir: Path, agent_dir: Path, tmp_dir: Path
    ) -> None:
        pi_settings = load_settings().harness_settings.pi
        # Session + isolated agent dir + per-review temp. SRT itself allow-writes
        # `/tmp/claude` (and sets the child's TMPDIR there) for its mux socket — we do
        # not grant all of `/tmp`. Writes are allow-only; do NOT also set denyWrite:["/"].
        allow_write = [
            str(session_dir.resolve()) + "/",
            str(agent_dir.resolve()) + "/",
            str(tmp_dir.resolve()) + "/",
        ]
        payload = {
            "network": {
                "allowedDomains": resolve_allowed_domains(pi_settings),
                "deniedDomains": [],
            },
            "filesystem": {
                "denyRead": [],
                "allowRead": [],
                "allowWrite": allow_write,
                "denyWrite": [],
            },
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def preflight(self) -> Preflight:
        # Both binaries are PATH-gated. Auth needs --model (provider-specific), so we can't probe
        # it without knowing which model a reviewer will pick — warn, matching copilot.
        pi_bin = which(self.binary)
        if pi_bin is None:
            return Preflight("fail", f"{self.binary} not found on PATH")
        if which(_SRT_BIN) is None:
            return Preflight(
                "fail", "srt not found on PATH (install @anthropic-ai/sandbox-runtime)"
            )
        # A cheap `pi --version` confirms the binary actually runs; still no auth.
        probe = run_sync([pi_bin, "--version"], timeout=AUTH_PROBE_TIMEOUT)
        if probe.returncode != 0:
            return Preflight("warn", f"pi present ({pi_bin}); could not run --version")
        return Preflight("warn", f"pi present ({pi_bin}); auth not verifiable")

    def _resolve_pi(self) -> str:
        resolved = which(self.binary)
        if resolved is None:
            raise AdapterError(f"pi binary not found: {self.binary}", transient=False)
        return resolved

    def _resolve_srt(self) -> str:
        resolved = which(_SRT_BIN)
        if resolved is None:
            raise AdapterError(
                "srt not found on PATH (install @anthropic-ai/sandbox-runtime)", transient=False
            )
        return resolved

    def _resolve_thinking(self, thinking: str | None) -> str | None:
        if not thinking or thinking == "default":
            return None
        if thinking not in _THINKING_LEVELS:
            raise AdapterError(
                f"pi thinking '{thinking}' invalid; use one of {sorted(_THINKING_LEVELS)}"
            )
        return thinking


async def _feed_stdin(stdin: asyncio.StreamWriter, prompt: str) -> None:
    stdin.write(prompt.encode("utf-8"))
    await stdin.drain()
    stdin.close()
    # wait_closed is not on every transport; ignore if missing.
    wait_closed = getattr(stdin, "wait_closed", None)
    if callable(wait_closed):
        await wait_closed()


async def _kill(proc: asyncio.subprocess.Process) -> None:
    # srt only forwards SIGINT/SIGTERM to the sandboxed child (and runs mux cleanup on
    # those handlers). SIGKILL the parent first and the tree is orphaned. TERM, then a
    # short grace, then KILL. Shield wait() so a cancelled timeout cannot skip reap.
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=2.0)
        return
    except TimeoutError:
        pass
    if proc.returncode is None:
        proc.kill()
        await asyncio.shield(proc.wait())


async def _read_jsonl(stream: asyncio.StreamReader, writer: EventLogWriter) -> list[dict]:
    """Read stdout as JSONL, tee each object to the live event log, return the parsed events.

    Split on `\\n` only (pi's JSON/RPC framing). A non-JSON line is teed as a raw string so a
    diagnostic still lands in review.log rather than aborting the parse.
    """
    events: list[dict] = []
    buf = b""
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        buf += chunk
        while True:
            nl = buf.find(b"\n")
            if nl == -1:
                break
            line, buf = buf[:nl], buf[nl + 1 :]
            if line.endswith(b"\r"):
                line = line[:-1]
            _consume_line(line, events, writer)
    if buf:
        _consume_line(buf, events, writer)
    return events


def _consume_line(line: bytes, events: list[dict], writer: EventLogWriter) -> None:
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        writer.append({"type": "aeview.non_json", "line": text})
        return
    writer.append(obj)
    if isinstance(obj, dict):
        events.append(obj)


def _last_assistant_text(events: list[dict]) -> str:
    """Last `message_end` whose message.role is assistant → concatenated text blocks."""
    text = ""
    for event in events:
        if event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = _assistant_text(message)
    return text


def _assistant_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            part = block.get("text")
            if isinstance(part, str):
                parts.append(part)
    return "".join(parts)


def _stream_error(events: list[dict]) -> str | None:
    """A completed stream can still be an error: last assistant stopReason=error."""
    for event in reversed(events):
        if event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        reason = message.get("stopReason")
        if reason in {"error", "aborted"}:
            return str(message.get("errorMessage") or reason)
        return None
    return None


def _usage_from_events(events: list[dict]) -> Usage:
    """Sum input/output tokens and USD cost across assistant messages in the stream.

    Prefer `message_end.message.usage` (authoritative); fall back to `message_update.usage`
    when a stream has no message_end (truncated). Cost is optional — missing → 0.0.
    """
    input_tokens = 0
    output_tokens = 0
    cost = 0.0
    seen_end = False
    for event in events:
        usage = None
        if event.get("type") == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                usage = message.get("usage")
                seen_end = True
        elif not seen_end and event.get("type") == "message_update":
            usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        input_tokens += int(usage.get("input") or 0)
        output_tokens += int(usage.get("output") or 0)
        cost_block = usage.get("cost")
        if isinstance(cost_block, dict):
            cost += float(cost_block.get("total") or 0.0)
        elif isinstance(usage.get("cost"), (int, float)):
            cost += float(usage["cost"])
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)
