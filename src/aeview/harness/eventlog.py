"""Live JSONL event-stream log for one harness invocation (review.log / dedup.log).

Each adapter tees the raw events its SDK emits (claude `Message`, codex `Notification`, copilot
`SessionEvent` — all dataclasses) into one JSONL file AS THEY ARRIVE, so a hung or timed-out
review still leaves a partial record on disk. The log is a write-only diagnostic artifact: nothing
reads it back (the report/findings come from the structured `review.json`), so the format is free
to be the raw stream. Full fidelity, no size cap — the read-anywhere policy lets a reviewer read
any file, and the log faithfully records what it saw (a user-chosen tradeoff).

One JSON object per line: {"seq": int, "ts": float, "kind": <kind>, "event": <obj>}. `kind`
discriminates a raw SDK event from aeview's own markers: "meta" (opening line: harness/model),
"event" (a raw SDK event, verbatim), "result"/"error" (the terminal outcome). The terminal line
captures the failure case in-stream too — a non-conforming answer is already in the events above.

Best-effort throughout: opening, serializing, or writing can never raise, so logging degrades
silently rather than breaking a review (the adapters' AdapterError-only failure contract + the
rule that a log write must not mask a real parsed result).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import time
from enum import Enum
from pathlib import Path
from typing import IO, Any

# Cap only the unserializable-fallback repr — real events are full-fidelity by design.
_REPR_CAP = 4096


def _json_default(obj: object) -> Any:
    # json.dumps calls this for anything it can't natively serialize. A dataclass (the raw SDK
    # events) → asdict, which recurses (nested dataclasses become dicts; enum/datetime/UUID leaves
    # fall to a later default call). Enum → its value; a nested pydantic model → its json dict;
    # anything else (datetime, UUID, ...) → str. Keeps every line valid JSON without per-type code.
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, Enum):
        return obj.value
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except Exception:  # noqa: BLE001 - a misbehaving model_dump degrades to repr, never raises
            return str(obj)
    return str(obj)


class EventLogWriter:
    """Live JSONL writer for one invocation's raw event stream: append-as-you-go + flush, so a
    wedged review leaves a partial log. Every method is best-effort and never raises."""

    def __init__(self, log_path: Path, *, harness: str, model: str) -> None:
        self._seq = 0
        self._fh: IO[str] | None = None
        # Open at the START of the review (not the end) so live events land immediately; create the
        # dir defensively since the writer can run before any other artifact in the review dir.
        with contextlib.suppress(OSError):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = log_path.open("w", encoding="utf-8")
        self._write("meta", {"harness": harness, "model": model})

    def append(self, event: object) -> None:
        """Tee one raw SDK event (a dataclass) as a JSONL line, live."""
        self._write("event", event)

    def result(self) -> None:
        """Terminal line: the invocation produced a usable result."""
        self._write("result", {"ok": True})

    def error(self, detail: str) -> None:
        """Terminal line: the invocation failed (the raw answer, if any, is in the events above)."""
        self._write("error", {"detail": detail})

    def close(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None

    def _write(self, kind: str, event: object) -> None:
        if self._fh is None:
            return
        seq = self._seq
        self._seq += 1
        record: dict[str, Any] = {"seq": seq, "ts": time.time(), "kind": kind, "event": event}
        try:
            text = json.dumps(record, default=_json_default)
        except Exception:  # noqa: BLE001 - one unserializable event must not break the stream
            record["event"] = {"type": "aeview.unserializable", "repr": repr(event)[:_REPR_CAP]}
            text = json.dumps(record, default=str)
        with contextlib.suppress(OSError):
            self._fh.write(text + "\n")
            self._fh.flush()
