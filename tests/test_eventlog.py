from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime
from enum import Enum

from aeview.harness.eventlog import EventLogWriter, _json_default


class _Color(Enum):
    RED = "red"


@dataclasses.dataclass
class _Inner:
    name: str
    when: datetime


@dataclasses.dataclass
class _Event:
    method: str
    inner: _Inner
    ids: list


def _lines(path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines()]


def test_meta_line_written_on_open(tmp_path):
    log = tmp_path / "review.log"
    EventLogWriter(log, harness="codex", model="gpt-5.5").close()
    lines = _lines(log)
    assert len(lines) == 1
    assert lines[0]["seq"] == 0
    assert lines[0]["kind"] == "meta"
    assert lines[0]["event"] == {"harness": "codex", "model": "gpt-5.5"}
    assert isinstance(lines[0]["ts"], float)


def test_append_then_result_terminal(tmp_path):
    log = tmp_path / "review.log"
    w = EventLogWriter(log, harness="codex", model="m")
    w.append(_Event("item.completed", _Inner("x", datetime.now(UTC)), [1, 2]))
    w.result()
    w.close()
    lines = _lines(log)
    assert [ln["kind"] for ln in lines] == ["meta", "event", "result"]
    assert [ln["seq"] for ln in lines] == [0, 1, 2]  # seq is monotonic across all lines
    ev = lines[1]["event"]
    assert ev["method"] == "item.completed"
    assert ev["inner"]["name"] == "x"  # nested dataclass is recursed by asdict
    assert isinstance(ev["inner"]["when"], str)  # datetime leaf → str
    assert ev["ids"] == [1, 2]
    assert lines[2]["event"] == {"ok": True}


def test_error_terminal(tmp_path):
    log = tmp_path / "review.log"
    w = EventLogWriter(log, harness="x", model="m")
    w.error("kaboom")
    w.close()
    last = _lines(log)[-1]
    assert last == {"seq": 1, "ts": last["ts"], "kind": "error", "event": {"detail": "kaboom"}}


def test_full_fidelity_no_cap(tmp_path):
    # Read-anywhere policy + user choice: no size cap. A large event payload is written whole.
    log = tmp_path / "review.log"
    big = "x" * 2_000_000
    w = EventLogWriter(log, harness="x", model="m")
    w.append(_Event("big", _Inner(big, datetime.now(UTC)), []))
    w.close()
    assert len(_lines(log)[1]["event"]["inner"]["name"]) == 2_000_000  # not truncated


def test_enum_and_uuid_serialized(tmp_path):
    @dataclasses.dataclass
    class E:
        color: _Color
        ident: uuid.UUID

    log = tmp_path / "review.log"
    u = uuid.uuid4()
    w = EventLogWriter(log, harness="x", model="m")
    w.append(E(_Color.RED, u))
    w.close()
    ev = _lines(log)[1]["event"]
    assert ev["color"] == "red"  # Enum → value
    assert ev["ident"] == str(u)  # UUID → str


def test_nested_pydantic_model_serialized(tmp_path):
    from pydantic import BaseModel

    class M(BaseModel):
        n: int

    @dataclasses.dataclass
    class E:
        m: M

    log = tmp_path / "review.log"
    w = EventLogWriter(log, harness="x", model="m")
    w.append(E(M(n=5)))
    w.close()
    assert _lines(log)[1]["event"]["m"] == {"n": 5}  # nested pydantic → model_dump


def test_unserializable_event_degrades_to_marker(tmp_path):
    # An event json.dumps can't encode (a tuple dict key) must not break the stream — it becomes an
    # aeview.unserializable marker line and writing keeps going.
    @dataclasses.dataclass
    class BadEvent:
        data: dict

    log = tmp_path / "review.log"
    w = EventLogWriter(log, harness="x", model="m")
    w.append(BadEvent(data={(1, 2): "tuple-key"}))
    w.append(_Event("after", _Inner("y", datetime.now(UTC)), []))
    w.close()
    lines = _lines(log)
    assert lines[1]["event"]["type"] == "aeview.unserializable"
    assert "BadEvent" in lines[1]["event"]["repr"]
    assert lines[2]["event"]["method"] == "after"  # the stream continued after the bad event


def test_bad_path_is_silent(tmp_path):
    # log_path is a directory → can't open → logging disabled; methods are no-ops, never raise.
    d = tmp_path / "adir"
    d.mkdir()
    w = EventLogWriter(d, harness="x", model="m")  # must not raise
    w.append(_Event("e", _Inner("n", datetime.now(UTC)), []))
    w.result()
    w.close()  # must not raise
    assert d.is_dir()


def test_appends_are_flushed_live(tmp_path):
    # Each line is flushed as written, so a reader sees events BEFORE close — the property that
    # gives a hung/timed-out review a partial log on disk.
    log = tmp_path / "review.log"
    w = EventLogWriter(log, harness="x", model="m")
    w.append(_Event("e1", _Inner("a", datetime.now(UTC)), []))
    assert [ln["kind"] for ln in _lines(log)] == ["meta", "event"]  # read before close()
    w.close()


def test_close_is_idempotent(tmp_path):
    log = tmp_path / "review.log"
    w = EventLogWriter(log, harness="x", model="m")
    w.close()
    w.close()  # second close must not raise


def test_json_default_leaf_types():
    assert _json_default(_Color.RED) == "red"
    u = uuid.uuid4()
    assert _json_default(u) == str(u)
    assert isinstance(_json_default(datetime.now(UTC)), str)


def test_creates_missing_parent_dir(tmp_path):
    # The writer opens at review start, possibly before any other artifact in the review dir, so it
    # creates the parent dir itself rather than silently disabling logging.
    log = tmp_path / "nope" / "deeper" / "review.log"
    w = EventLogWriter(log, harness="x", model="m")
    w.append(_Event("e", _Inner("a", datetime.now(UTC)), []))
    w.close()
    assert log.exists()
    assert _lines(log)[0]["kind"] == "meta"


def test_json_default_model_dump_raises_falls_back_to_str():
    class M:
        def model_dump(self, *a, **k):
            raise RuntimeError("boom")

        def __str__(self):
            return "M-as-str"

    assert _json_default(M()) == "M-as-str"  # raising model_dump degrades to str, never propagates


def test_repr_raising_fallback_does_not_propagate(tmp_path):
    # The unserializable fallback's repr() is itself guarded: an event whose __repr__ raises must
    # not break the never-raises contract — the line degrades to "<unrepresentable>".
    class Boom:
        def __repr__(self) -> str:
            raise RuntimeError("repr boom")

    log = tmp_path / "review.log"
    w = EventLogWriter(log, harness="x", model="m")
    w.append(Boom())  # must not raise despite a raising __repr__/__str__
    w.append(_Event("after", _Inner("y", datetime.now(UTC)), []))
    w.close()
    lines = _lines(log)
    assert lines[1]["event"]["type"] == "aeview.unserializable"
    assert lines[1]["event"]["repr"] == "<unrepresentable>"
    assert lines[2]["event"]["method"] == "after"  # the stream continued past the bad event


def test_write_oserror_is_suppressed(tmp_path):
    # A write/flush OSError mid-stream (e.g. disk full) is suppressed — best-effort logging must not
    # raise into the review. (Open failure is covered separately by test_bad_path_is_silent.)
    class BrokenFH:
        def write(self, _s: str) -> int:
            raise OSError("disk full")

        def flush(self) -> None: ...

        def close(self) -> None: ...

    log = tmp_path / "review.log"
    w = EventLogWriter(log, harness="x", model="m")
    w._fh = BrokenFH()  # type: ignore[assignment]  # simulate a mid-stream write failure
    w.append(_Event("e", _Inner("a", datetime.now(UTC)), []))  # must not raise
    w.close()
