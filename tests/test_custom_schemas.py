"""custom-schemas: per-reviewer finding output shape (override/drop the descriptive slots).

Covers the whole slice at the unit level: frontmatter parse + fragment resolution/bounds, schema
composition, the schema-derived validator, the loose finding carrier round-trip, merge/report
tolerance of reshaped/dropped slots, and the run-dir freeze + resume re-read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeview import merge as merge_mod
from aeview.config import HarnessInstance, Settings
from aeview.dedup import DedupOutcome
from aeview.fanout import fan_out
from aeview.github import _finding_md
from aeview.harness.base import HarnessOutput, StructuredOutput
from aeview.harness.claude_code import ClaudeCodeAdapter
from aeview.merge import merge_reviews
from aeview.report import render_human
from aeview.resolve import ResolveError, resolve_reviewer
from aeview.runstore import RunStore, new_run_id
from aeview.schema import (
    Coverage,
    Dedup,
    DuplicateGroup,
    Finding,
    MergedFinding,
    PooledFinding,
    Report,
    ReviewOutput,
    ReviewResult,
    RosterEntry,
    Usage,
    UsageBreakdown,
    build_review_validator,
    compose_review_schema,
    review_output_json_schema,
)

_RUBRIC = {
    "type": "object",
    "properties": {"readability": {"type": "integer", "minimum": 1, "maximum": 5}},
    "required": ["readability"],
}


def _settings() -> Settings:
    return Settings(fallback_reviewer_harnesses=[HarnessInstance(harness="claude-code", model="m")])


def _write_reviewer(base: Path, name: str, frontmatter: str, files: dict[str, str] | None = None):
    d = base / ".aeview" / "reviewers" / name
    d.mkdir(parents=True)
    (d / "REVIEWER.md").write_text(f"---\n{frontmatter}\n---\nbody\n")
    for rel, content in (files or {}).items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return d


# --- frontmatter parse + fragment resolution ------------------------------------------------


def test_inline_and_path_fragments_resolve(tmp_path):
    _write_reviewer(
        tmp_path,
        "rubricator",
        "\n".join(
            [
                "name: rubricator",
                "harnesses: [{harness: claude-code, model: m}]",
                "custom-schemas:",
                "  category: {type: string, enum: [perf, a11y]}",
                "  body: ./schemas/rubric.json",
                "  recommendation: null",
            ]
        ),
        files={"schemas/rubric.json": json.dumps(_RUBRIC)},
    )
    r = resolve_reviewer("rubricator", tmp_path, _settings())
    assert r.custom_schemas["category"] == {"type": "string", "enum": ["perf", "a11y"]}
    assert r.custom_schemas["body"] == _RUBRIC  # path was read + parsed
    assert r.custom_schemas["recommendation"] is None  # null == drop


def test_no_custom_schemas_is_empty(tmp_path):
    _write_reviewer(tmp_path, "plain", "name: plain\nharnesses: [{harness: claude-code, model: m}]")
    assert resolve_reviewer("plain", tmp_path, _settings()).custom_schemas == {}


def test_unknown_slot_key_rejected(tmp_path):
    # The skeleton is never customizable; only the four descriptive slots are accepted keys.
    _write_reviewer(tmp_path, "bad", "name: bad\ncustom-schemas:\n  severity: {type: string}")
    with pytest.raises(ResolveError, match="custom-schemas keys must be among"):
        resolve_reviewer("bad", tmp_path, _settings())


def test_bad_value_type_rejected(tmp_path):
    _write_reviewer(tmp_path, "bad", "name: bad\ncustom-schemas:\n  title: 5")
    with pytest.raises(ResolveError, match="must be a JSON Schema object"):
        resolve_reviewer("bad", tmp_path, _settings())


def test_ref_in_fragment_rejected(tmp_path):
    _write_reviewer(tmp_path, "bad", "name: bad\ncustom-schemas:\n  body: {$ref: '#/$defs/X'}")
    with pytest.raises(ResolveError, match=r"\$ref is not supported"):
        resolve_reviewer("bad", tmp_path, _settings())


def test_missing_schema_file_rejected(tmp_path):
    _write_reviewer(tmp_path, "bad", "name: bad\ncustom-schemas:\n  body: ./nope.json")
    with pytest.raises(ResolveError, match="cannot read schema file"):
        resolve_reviewer("bad", tmp_path, _settings())


def test_oversized_fragment_rejected(tmp_path):
    deep = current = {"type": "object", "properties": {}}
    for _ in range(20):  # exceeds _MAX_FRAGMENT_DEPTH
        child: dict = {"type": "object", "properties": {}}
        current["properties"]["x"] = child
        current = child
    _write_reviewer(
        tmp_path,
        "bad",
        "name: bad\ncustom-schemas:\n  body: ./d.json",
        files={"d.json": json.dumps(deep)},
    )
    with pytest.raises(ResolveError, match="nests deeper than"):
        resolve_reviewer("bad", tmp_path, _settings())


# --- schema composition ----------------------------------------------------------------------


def _finding_props(schema: dict) -> dict:
    ref = schema["properties"]["findings"]["items"]["$ref"].rsplit("/", 1)[-1]
    return schema["$defs"][ref]


def test_compose_none_is_the_default_schema():
    assert compose_review_schema(None) == review_output_json_schema()
    assert compose_review_schema({}) == review_output_json_schema()


def test_compose_override_drop_and_enum():
    schema = compose_review_schema(
        {"body": _RUBRIC, "recommendation": None, "category": {"type": "string", "enum": ["p"]}}
    )
    fd = _finding_props(schema)
    assert fd["properties"]["body"]["type"] == "object"
    assert "recommendation" not in fd["properties"]
    assert "recommendation" not in fd["required"]
    assert fd["properties"]["category"]["enum"] == ["p"]
    # skeleton untouched
    for field in ("severity", "confidence", "location"):
        assert field in fd["properties"] and field in fd["required"]


def test_compose_does_not_mutate_default():
    compose_review_schema({"recommendation": None})
    assert "recommendation" in _finding_props(review_output_json_schema())["properties"]


# --- validator -------------------------------------------------------------------------------


def _review(findings: list[dict]) -> dict:
    return {"verdict": "needs-attention", "summary": "s", "findings": findings, "next_steps": []}


def _default_finding(**over) -> dict:
    f = {
        "title": "t",
        "body": "b",
        "severity": "high",
        "category": "bug",
        "confidence": 0.5,
        "location": {"file": "a.py", "line_start": 1, "line_end": 1},
        "recommendation": "fix",
    }
    f.update(over)
    return f


def test_default_validator_is_strict():
    # No custom-schemas => the strict built-in contract (byte-for-byte the pre-feature validation),
    # so a skeleton-only finding is rejected via pydantic ("Field required", a ValueError subclass).
    v = build_review_validator(review_output_json_schema())
    v(_review([_default_finding()]))  # ok
    skeleton_only = {
        "severity": "low",
        "confidence": 0.5,
        "location": {"file": "a", "line_start": 1, "line_end": 1},
    }
    with pytest.raises(ValueError, match="required"):
        v(_review([skeleton_only]))


def test_custom_validator_requires_surviving_slots():
    # A customized schema (only category overridden) still requires the other slots; the loose
    # validator's presence check fires with its own message when one is missing.
    schema = compose_review_schema({"category": {"type": "string", "enum": ["x"]}})
    v = build_review_validator(schema)
    finding = _default_finding(category="x")
    del finding["body"]
    with pytest.raises(ValueError, match="missing required field"):
        v(_review([finding]))


def test_default_validator_rejects_bad_skeleton():
    v = build_review_validator(review_output_json_schema())
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError on bad confidence
        v(_review([_default_finding(confidence=5.0)]))


def test_custom_validator_accepts_reshaped_and_drops():
    schema = compose_review_schema({"body": _RUBRIC, "recommendation": None})
    v = build_review_validator(schema)
    # body is now an object and recommendation is dropped (absent) — still valid.
    finding = _default_finding(body={"readability": 4})
    del finding["recommendation"]
    v(_review([finding]))


# --- loose carrier round-trip ----------------------------------------------------------------


def _extra(model) -> dict:
    """The finding's non-skeleton slots (they ride in model_extra on the loose carrier)."""
    extra = model.model_extra
    assert extra is not None
    return extra


def test_finding_carries_reshaped_and_extra_slots():
    f = Finding.model_validate(
        {
            "severity": "low",
            "confidence": 0.4,
            "location": {"file": "a.py", "line_start": 2, "line_end": 3},
            "title": "t",
            "body": {"readability": 4},  # reshaped to an object
            "score": 7,  # a brand-new field the reviewer's schema added
        }
    )
    assert f.severity == "low"  # skeleton stays typed
    assert f.location.line_end == 3
    dumped = f.model_dump()
    assert dumped["body"] == {"readability": 4}
    assert dumped["score"] == 7
    # survives the pool/merge reconstruction (the **model_dump() pattern in merge.py)
    pf = PooledFinding(id="f1", **f.model_dump())
    assert _extra(pf)["body"] == {"readability": 4}
    mf = MergedFinding(id="f1", **f.model_dump())
    assert _extra(mf)["score"] == 7


async def test_merge_preserves_reshaped_body(aeview_home):
    store = RunStore.create(new_run_id())
    finding = Finding.model_validate(
        {
            "severity": "medium",
            "confidence": 0.9,
            "location": {"file": "a.py", "line_start": 1, "line_end": 1},
            "title": "rubric finding",
            "body": {"readability": 2, "coverage": 5},
        }
    )
    result = ReviewResult(
        id="r__claude-code-m",
        reviewer="r",
        harness="claude-code",
        model="m",
        status="done",
        verdict="needs-attention",
        summary="s",
        findings=[finding],
        next_steps=[],
    )
    report = await merge_reviews([result], Settings(), store, aeview_home)
    assert _extra(report.findings[0])["body"] == {"readability": 2, "coverage": 5}
    assert report.findings[0].severity == "medium"


# --- report rendering with dropped slots -----------------------------------------------------


def test_render_human_tolerates_dropped_title_and_recommendation():
    finding = MergedFinding.model_validate(
        {
            "id": "f1",
            "severity": "high",
            "confidence": 0.8,
            "location": {"file": "a.py", "line_start": 5, "line_end": 5},
            # title + recommendation dropped by this reviewer's custom-schemas
            "sources": [{"review": "r__x", "severity": "high", "confidence": 0.8}],
            "agreement": 1,
        }
    )
    report = Report(
        verdict="needs-attention",
        summary="1 finding",
        findings=[finding],
        coverage=Coverage(contributed=1, failed=0),
        dedup=Dedup(status="skipped"),
        usage=UsageBreakdown(),
    )
    text = render_human(report)
    assert "(untitled)" in text  # no title -> graceful placeholder
    assert "a.py:5" in text  # location-only line, no ' :: ' recommendation suffix
    assert " :: " not in text


# --- freeze + fan-out delivery ----------------------------------------------------------------


class _CaptureSchemaAdapter:
    def __init__(self) -> None:
        self.schema: object = "unset"

    async def run(self, prompt, model, cwd, log_path, thinking=None, timeout=None, schema=None):
        self.schema = schema
        return HarnessOutput(
            review=ReviewOutput(verdict="approve", summary="ok", findings=[], next_steps=[]),
            usage=Usage(),
            raw="{}",
        )


async def test_fan_out_delivers_per_reviewer_schema(aeview_home, monkeypatch):
    from aeview import fanout

    adapter = _CaptureSchemaAdapter()
    monkeypatch.setattr(fanout, "get_adapter", lambda h, override=None: adapter)
    store = RunStore.create(new_run_id())
    entry = RosterEntry(id="r__claude-code-m", reviewer="r", harness="claude-code", model="m")
    custom = compose_review_schema({"recommendation": None})
    await fan_out(store, [entry], {"r": "p"}, aeview_home, schema_by_reviewer={"r": custom})
    assert adapter.schema == custom


def test_review_schema_freeze_round_trips(aeview_home):
    store = RunStore.create(new_run_id())
    schema = compose_review_schema({"category": {"type": "string", "enum": ["x"]}})
    store.write_review_schema("r", schema)
    assert store.read_review_schema("r") == schema


# --- adapter delegation, dedup survivor, downstream rendering (cycle-1 fixes) -----------------


async def test_adapter_run_delivers_custom_schema_and_builds_carrier(tmp_path, monkeypatch):
    # run() delegates to run_review: the per-reviewer schema reaches run_structured, a
    # schema-derived validator is applied, and the loose ReviewOutput carrier is built.
    adapter = ClaudeCodeAdapter()
    captured: dict = {}

    async def fake_run_structured(
        prompt, schema, model, cwd, log_path, thinking=None, timeout=None, validate=None
    ):
        captured["schema"] = schema
        payload = {"verdict": "approve", "summary": "ok", "findings": [], "next_steps": []}
        if validate is not None:
            validate(payload)
        return StructuredOutput(payload=payload, usage=Usage(), raw="{}")

    monkeypatch.setattr(adapter, "run_structured", fake_run_structured)
    custom = compose_review_schema({"recommendation": None})
    out = await adapter.run("p", "m", tmp_path, tmp_path / "log", schema=custom)
    assert captured["schema"] == custom
    assert out.review.verdict == "approve"


async def test_dedup_survivor_keeps_reshaped_body(aeview_home, monkeypatch):
    # When dedup merges two reshaped findings, the survivor is kept verbatim — its rubric-object
    # body carries through the loose Pooled/Merged carriers (option 1: no cross-source aggregation).
    def _rubric_review(rid: str) -> ReviewResult:
        finding = Finding.model_validate(
            {
                "severity": "high",
                "confidence": 0.9,
                "location": {"file": "a.py", "line_start": 1, "line_end": 1},
                "title": "same issue",
                "body": {"readability": 2, "risk": 4},
            }
        )
        return ReviewResult(
            id=rid,
            reviewer="r",
            harness="claude-code",
            model="m",
            status="done",
            verdict="needs-attention",
            summary="s",
            findings=[finding],
            next_steps=[],
        )

    async def fake_run_dedup(pool, instance, store, cwd, timeout, binary_override=None):
        return DedupOutcome("ok", [DuplicateGroup(survivor="f1", duplicates=["f2"])], Usage(), "h")

    monkeypatch.setattr(merge_mod, "run_dedup", fake_run_dedup)
    settings = Settings(deduplication_harness=HarnessInstance(harness="claude-code", model="m"))
    store = RunStore.create(new_run_id())
    report = await merge_reviews(
        [_rubric_review("r__claude-code-a"), _rubric_review("r__codex-b")],
        settings,
        store,
        aeview_home,
    )
    assert len(report.findings) == 1
    assert report.findings[0].agreement == 2
    assert _extra(report.findings[0])["body"] == {"readability": 2, "risk": 4}


def test_github_finding_md_tolerates_dropped_and_reshaped_slots():
    # PR posting must not crash when a reviewer reshaped body to an object and dropped the rest.
    finding = MergedFinding.model_validate(
        {
            "id": "f1",
            "severity": "high",
            "confidence": 0.8,
            "location": {"file": "a.py", "line_start": 5, "line_end": 5},
            "body": {"readability": 3},  # object, not a string
            # title / recommendation / category dropped
            "sources": [{"review": "r__x", "severity": "high", "confidence": 0.8}],
            "agreement": 1,
        }
    )
    md = _finding_md(finding, "run1", show_location=True)  # must not raise on .strip()
    assert "(untitled)" in md
    assert '"readability": 3' in md  # object body rendered as JSON


def test_render_human_renders_present_slots():
    finding = MergedFinding.model_validate(
        {
            "id": "f1",
            "severity": "medium",
            "confidence": 0.5,
            "location": {"file": "a.py", "line_start": 2, "line_end": 4},
            "title": "the title",
            "recommendation": "do the fix",
            "sources": [{"review": "r__x", "severity": "medium", "confidence": 0.5}],
            "agreement": 1,
        }
    )
    report = Report(
        verdict="needs-attention",
        summary="s",
        findings=[finding],
        coverage=Coverage(contributed=1, failed=0),
        dedup=Dedup(status="skipped"),
        usage=UsageBreakdown(),
    )
    text = render_human(report)
    assert "the title" in text
    assert "a.py:2-4 :: do the fix" in text


# --- fragment sanitization edges -------------------------------------------------------------


def test_node_count_cap_rejected(tmp_path):
    big = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(600)}}
    _write_reviewer(
        tmp_path,
        "bad",
        "name: bad\ncustom-schemas:\n  body: ./b.json",
        files={"b.json": json.dumps(big)},
    )
    with pytest.raises(ResolveError, match="more than"):
        resolve_reviewer("bad", tmp_path, _settings())


def test_invalid_json_file_rejected(tmp_path):
    _write_reviewer(
        tmp_path, "bad", "name: bad\ncustom-schemas:\n  body: ./b.json", files={"b.json": "{not"}
    )
    with pytest.raises(ResolveError, match="not valid JSON"):
        resolve_reviewer("bad", tmp_path, _settings())


def test_non_object_json_file_rejected(tmp_path):
    _write_reviewer(
        tmp_path, "bad", "name: bad\ncustom-schemas:\n  body: ./b.json", files={"b.json": "[1,2]"}
    )
    with pytest.raises(ResolveError, match="must contain a JSON Schema object"):
        resolve_reviewer("bad", tmp_path, _settings())
