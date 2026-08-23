"""custom-schemas: per-reviewer finding output shape (override/drop the descriptive slots).

Covers the whole slice at the unit level: frontmatter parse + fragment resolution/bounds, schema
composition, the schema-derived validator, the loose finding carrier round-trip, merge/report
tolerance of reshaped/dropped slots, and the run-dir freeze + resume re-read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError
from typer.testing import CliRunner

from aeview import merge as merge_mod
from aeview.cli import app
from aeview.config import HarnessInstance, Settings
from aeview.dedup import DedupOutcome
from aeview.fanout import fan_out
from aeview.github import _finding_md
from aeview.harness.base import HarnessOutput, StructuredOutput
from aeview.harness.claude_code import ClaudeCodeAdapter
from aeview.harness.codex import CodexAdapter
from aeview.harness.copilot import CopilotAdapter
from aeview.harness.pi import PiAdapter
from aeview.merge import merge_reviews
from aeview.report import render_human
from aeview.resolve import ResolveError, resolve_reviewer
from aeview.runstore import RunStore, latest_run_id, new_run_id
from aeview.schema import (
    Coverage,
    Dedup,
    DuplicateGroup,
    Finding,
    Invocation,
    MergedFinding,
    PooledFinding,
    Report,
    ReviewOutput,
    ReviewResult,
    RosterEntry,
    RunManifest,
    ScopeSpec,
    Usage,
    UsageBreakdown,
    build_review_validator,
    compose_review_schema,
    make_strict_schema,
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
    # No custom-schemas => the built-in schema; a skeleton-only finding is missing required slots.
    v = build_review_validator(review_output_json_schema())
    v(_review([_default_finding()]))  # ok
    skeleton_only = {
        "severity": "low",
        "confidence": 0.5,
        "location": {"file": "a", "line_start": 1, "line_end": 1},
    }
    with pytest.raises(ValidationError, match="required"):
        v(_review([skeleton_only]))


def test_custom_validator_requires_surviving_slots():
    # A customized schema (only category overridden) still requires the other slots.
    schema = compose_review_schema({"category": {"type": "string", "enum": ["x"]}})
    v = build_review_validator(schema)
    finding = _default_finding(category="x")
    del finding["body"]
    with pytest.raises(ValidationError, match="required"):
        v(_review([finding]))


def test_default_validator_rejects_bad_skeleton():
    v = build_review_validator(review_output_json_schema())
    with pytest.raises(ValidationError, match="maximum"):  # confidence 5.0 > 1.0
        v(_review([_default_finding(confidence=5.0)]))


def test_custom_validator_enforces_skeleton():
    # Even under a customized schema the skeleton is still validated.
    v = build_review_validator(
        compose_review_schema({"category": {"type": "string", "enum": ["x"]}})
    )
    with pytest.raises(ValidationError, match="maximum"):
        v(_review([_default_finding(category="x", confidence=5.0)]))


def test_custom_validator_accepts_reshaped_and_drops():
    schema = compose_review_schema({"body": _RUBRIC, "recommendation": None})
    v = build_review_validator(schema)
    # body is now an object and recommendation is dropped (absent) — still valid.
    finding = _default_finding(body={"readability": 4})
    del finding["recommendation"]
    v(_review([finding]))


def test_custom_validator_enforces_fragment_inner_shape():
    # The point of the jsonschema validator: a custom fragment's inner constraints are enforced,
    # not just field presence (readability is bounded 1-5 by _RUBRIC).
    v = build_review_validator(compose_review_schema({"body": _RUBRIC}))
    with pytest.raises(ValidationError, match="maximum"):
        v(_review([_default_finding(body={"readability": 99})]))


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


async def test_fan_out_delivers_distinct_schema_per_reviewer(aeview_home, monkeypatch):
    from aeview import fanout

    seen: dict = {}

    class _Rec:
        async def run(self, prompt, model, cwd, log_path, thinking=None, timeout=None, schema=None):
            seen[prompt] = schema  # each reviewer has a distinct prompt
            return HarnessOutput(
                review=ReviewOutput(verdict="approve", summary="ok", findings=[], next_steps=[]),
                usage=Usage(),
                raw="{}",
            )

    monkeypatch.setattr(fanout, "get_adapter", lambda h, override=None: _Rec())
    store = RunStore.create(new_run_id())
    entries = [
        RosterEntry(id="r1__claude-code-m", reviewer="r1", harness="claude-code", model="m"),
        RosterEntry(id="r2__codex-m", reviewer="r2", harness="codex", model="m"),
    ]
    s1 = compose_review_schema({"recommendation": None})
    s2 = compose_review_schema({"category": {"type": "string", "enum": ["x"]}})
    await fan_out(
        store,
        entries,
        {"r1": "p1", "r2": "p2"},
        aeview_home,
        schema_by_reviewer={"r1": s1, "r2": s2},
    )
    assert seen == {"p1": s1, "p2": s2}  # each reviewer got its own schema, not the last one


def test_review_schema_freeze_round_trips(aeview_home):
    store = RunStore.create(new_run_id())
    schema = compose_review_schema({"category": {"type": "string", "enum": ["x"]}})
    store.write_review_schema("r", schema)
    assert store.read_review_schema("r") == schema


# --- adapter delegation, dedup survivor, downstream rendering (cycle-1 fixes) -----------------


@pytest.mark.parametrize(
    "adapter_cls", [ClaudeCodeAdapter, CodexAdapter, CopilotAdapter, PiAdapter]
)
async def test_adapter_run_delivers_custom_schema_and_builds_carrier(
    adapter_cls, tmp_path, monkeypatch
):
    # Every adapter's run() delegates to the shared run_review: the per-reviewer schema reaches
    # run_structured, a schema-derived validator is applied, and the loose ReviewOutput carrier
    # is built. Parametrized so a delegation typo in any one adapter is caught.
    adapter = adapter_cls()
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


def test_render_human_stringifies_reshaped_slots():
    # A reviewer that reshaped title/body into objects must still render (f-string str), not crash.
    finding = MergedFinding.model_validate(
        {
            "id": "f1",
            "severity": "low",
            "confidence": 0.5,
            "location": {"file": "a.py", "line_start": 1, "line_end": 1},
            "title": {"score": 4},
            "body": {"readability": 3},
            "sources": [{"review": "r__x", "severity": "low", "confidence": 0.5}],
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
    assert "score" in text  # the reshaped object title is stringified inline


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


# --- merge robustness + freeze/resume wiring -------------------------------------------------


async def test_merge_survives_reserved_key_collision(aeview_home):
    # extra="allow" means a drifting harness could emit a finding key named id/sources/agreement;
    # merge must strip those (aeview's provenance wins), not TypeError on `Model(id=fid, ...)`.
    finding = Finding.model_validate(
        {
            "severity": "low",
            "confidence": 0.5,
            "location": {"file": "a.py", "line_start": 1, "line_end": 1},
            "title": "t",
            "id": "harness-supplied",
            "agreement": 99,
            "sources": ["junk"],
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
    store = RunStore.create(new_run_id())
    report = await merge_reviews([result], Settings(), store, aeview_home)
    assert len(report.findings) == 1
    assert report.findings[0].id == "f1"  # aeview's run-local id, not the harness-supplied one
    assert report.findings[0].agreement == 1  # aeview's, not the emitted 99
    # sources is aeview's provenance, not the harness-supplied junk
    assert [s.review for s in report.findings[0].sources] == ["r__claude-code-m"]


def test_run_freezes_composed_schema(aeview_home, git_repo, stub_claude, monkeypatch):
    # A real `run` composes each reviewer's schema and freezes it to the run dir, so resume can
    # reuse it. stub_claude returns a default-shaped review; the loose custom validator accepts it.
    d = git_repo / ".aeview" / "reviewers" / "cs"
    d.mkdir(parents=True)
    (d / "REVIEWER.md").write_text(
        "---\n"
        "name: cs\n"
        "harnesses: [{harness: claude-code, model: opus}]\n"
        "custom-schemas:\n"
        "  category: {type: string, enum: [x]}\n"
        "  recommendation: null\n"
        "---\nreview it\n"
    )
    monkeypatch.chdir(git_repo)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    CliRunner().invoke(app, ["run", "--reviewers", "cs", "--scope", "working-tree"])
    rid = latest_run_id()
    assert rid is not None
    expected = compose_review_schema(
        {"category": {"type": "string", "enum": ["x"]}, "recommendation": None}
    )
    assert RunStore(rid).read_review_schema("cs") == expected


def test_resume_reuses_frozen_custom_schema(aeview_home, monkeypatch):
    # resume must re-read the frozen schema and hand it to fan_out, not recompose from REVIEWER.md.
    import aeview.cli as cli

    store = RunStore.create("cs")
    custom = compose_review_schema({"body": _RUBRIC, "recommendation": None})
    store.write_review_schema("r", custom)
    store.write_prompt("r", "P")
    store.write_manifest(
        RunManifest(
            run_id="cs",
            created_at="2026-08-01T00:00:00Z",
            overall="interrupted",  # terminal, so resume proceeds
            invocation=Invocation(reviewers=["r"], scope=ScopeSpec(type="working-tree")),
            roster=[
                RosterEntry(id="r__claude-code-m", reviewer="r", harness="claude-code", model="m")
            ],
            dedup=None,
        )
    )
    store.write_review(
        ReviewResult(
            id="r__claude-code-m", reviewer="r", harness="claude-code", model="m", status="failed"
        )
    )
    captured: dict = {}

    async def fake_fan_out(
        s,
        roster,
        prompts,
        cwd,
        timeout=None,
        override_harness_binaries=None,
        schema_by_reviewer=None,
    ):
        captured["schemas"] = schema_by_reviewer
        return []

    monkeypatch.setattr(cli, "fan_out", fake_fan_out)
    CliRunner().invoke(app, ["resume", "cs"])
    assert captured["schemas"] == {"r": custom}


# --- path-traversal + PR-posting bounds (cycle-4 fixes) --------------------------------------


def test_schema_path_traversal_rejected(tmp_path):
    # A `..`-escaping path must not read a file outside the reviewer dir into the schema/prompt.
    _write_reviewer(tmp_path, "bad", "name: bad\ncustom-schemas:\n  body: ../../../../etc/passwd")
    with pytest.raises(ResolveError, match="escapes the reviewer directory"):
        resolve_reviewer("bad", tmp_path, _settings())


def test_absolute_schema_path_rejected(tmp_path):
    _write_reviewer(tmp_path, "bad", "name: bad\ncustom-schemas:\n  body: /etc/passwd")
    with pytest.raises(ResolveError, match="escapes the reviewer directory"):
        resolve_reviewer("bad", tmp_path, _settings())


def test_github_finding_md_clips_unbounded_custom_title():
    # A custom title has no schema length cap, so PR rendering must clip it like body/rec.
    finding = MergedFinding.model_validate(
        {
            "id": "f1",
            "severity": "low",
            "confidence": 0.5,
            "location": {"file": "a.py", "line_start": 1, "line_end": 1},
            "title": "x" * 5000,
            "sources": [{"review": "r__x", "severity": "low", "confidence": 0.5}],
            "agreement": 1,
        }
    )
    md = _finding_md(finding, "run1", show_location=False)
    assert "truncated" in md  # the unbounded custom title was length-capped before posting


def test_github_finding_md_renders_present_custom_category():
    finding = MergedFinding.model_validate(
        {
            "id": "f1",
            "severity": "low",
            "confidence": 0.5,
            "location": {"file": "a.py", "line_start": 1, "line_end": 1},
            "title": "t",
            "category": "perf",  # a custom category value
            "sources": [{"review": "r__x", "severity": "low", "confidence": 0.5}],
            "agreement": 1,
        }
    )
    md = _finding_md(finding, "run1", show_location=False)
    assert "perf" in md  # a present custom category is rendered in the header


def test_make_strict_schema_on_composed_custom_schema():
    # The codex constrained path strictifies the composed schema; a custom fragment must survive:
    # every object required + additionalProperties:false, recursively, so codex accepts it.
    schema = make_strict_schema(compose_review_schema({"body": _RUBRIC, "recommendation": None}))
    fd = _finding_props(schema)
    assert fd["additionalProperties"] is False
    assert set(fd["required"]) == set(fd["properties"])  # recommendation dropped, the rest required
    assert "recommendation" not in fd["properties"]
    body = fd["properties"]["body"]  # the inlined rubric object is strictified too
    assert body["additionalProperties"] is False
    assert set(body["required"]) == set(body["properties"])


def test_resume_tolerates_corrupt_frozen_schema(aeview_home, monkeypatch):
    # A corrupt schema.json (bad JSON) must not crash resume — it falls back to the default (omit).
    import aeview.cli as cli

    store = RunStore.create("cx")
    store.write_prompt("r", "P")
    (store.reviewers_dir / "r" / "schema.json").write_text("{not json")
    store.write_manifest(
        RunManifest(
            run_id="cx",
            created_at="2026-08-01T00:00:00Z",
            overall="interrupted",
            invocation=Invocation(reviewers=["r"], scope=ScopeSpec(type="working-tree")),
            roster=[
                RosterEntry(id="r__claude-code-m", reviewer="r", harness="claude-code", model="m")
            ],
            dedup=None,
        )
    )
    store.write_review(
        ReviewResult(
            id="r__claude-code-m", reviewer="r", harness="claude-code", model="m", status="failed"
        )
    )
    captured: dict = {}

    async def fake_fan_out(
        s,
        roster,
        prompts,
        cwd,
        timeout=None,
        override_harness_binaries=None,
        schema_by_reviewer=None,
    ):
        captured["schemas"] = schema_by_reviewer
        return []

    monkeypatch.setattr(cli, "fan_out", fake_fan_out)
    CliRunner().invoke(app, ["resume", "cx"])
    assert captured["schemas"] == {}  # corrupt schema omitted -> default fallback, no crash
