"""Canonical data models for findings, per-review results, runs, and reports.

These models are the contract that flows through the whole pipeline:
- `ReviewOutput` is the shape every harness must emit (and the JSON Schema we hand
  to harnesses that support structured output).
- `ReviewResult` is what a worker persists to `reviewers/<reviewer>/<instance>/review.json`.
- `Report` is the merged, deduplicated artifact written last to `report.json`.

JSON is snake_case throughout for consistency (user-facing `settings.json` is the
only camelCase surface; see `config.py`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal["bug", "security", "regression", "test_gap", "maintainability"]
Verdict = Literal["approve", "needs-attention"]
ReviewStatus = Literal["pending", "running", "done", "failed"]
RunState = Literal["running", "done", "failed", "interrupted"]
DedupState = Literal["ok", "skipped", "failed"]

# The finding skeleton — the fields the merge/report/dedup mechanics read (sort, verdict, survivor
# choice, corroboration, location). It is strictly typed and can NEVER be customized: a reviewer's
# `custom-schemas` may only reshape or drop the descriptive slots below, never these.
SKELETON_FIELDS = ("severity", "confidence", "location")
# The descriptive slots a reviewer may override (with a JSON Schema fragment) or drop (null) via
# `custom-schemas`. A closed set: everything else in a finding is either the skeleton or internal.
CUSTOMIZABLE_SLOTS = ("title", "body", "recommendation", "category")


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    line_start: int = Field(ge=0)
    line_end: int = Field(ge=0)


# Strict, private schema-source models: they generate the default JSON schema
# (`compose_review_schema` with no overrides) and strictly post-validate a default reviewer. They
# are NOT carriers — findings are stored in the looser `Finding` below. Their name/docstring becomes
# the model-facing schema `title`/`description`, so both are pinned to the clean pre-custom-schemas
# text (via `title=` + a user-facing docstring) — never implementation notes, which would leak into
# the schema every harness sees. (Only the internal `$defs` key differs from the old schema;
# harnesses resolve `$ref` regardless.)
class _DefaultFinding(BaseModel):
    """A single issue as emitted by a reviewer (no provenance yet)."""

    model_config = ConfigDict(extra="forbid", title="Finding")

    title: str = Field(min_length=1, max_length=140)
    body: str
    severity: Severity
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    location: Location
    recommendation: str


class _DefaultReviewOutput(BaseModel):
    """The structured output contract for a single harness invocation."""

    model_config = ConfigDict(extra="forbid", title="ReviewOutput")

    verdict: Verdict
    summary: str
    findings: list[_DefaultFinding] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    """A single issue as emitted by a reviewer (no provenance yet).

    The skeleton (`SKELETON_FIELDS`) is strictly typed — every merge/report/dedup mechanic reads
    only these, so they must always be present and well-formed. The descriptive slots
    (`CUSTOMIZABLE_SLOTS`) and any reviewer-defined fields ride as extra (`extra="allow"`): a
    reviewer's `custom-schemas` reshapes or drops them, and the per-reviewer JSON schema — not this
    model — enforces their real shape at the harness boundary. This is why the pipeline is immune to
    slot customization: the carrier only ever type-checks the skeleton and faithfully carries the
    rest (title/body/recommendation/category live in `model_extra`)."""

    model_config = ConfigDict(extra="allow")

    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    location: Location

    def slot(self, key: str) -> object | None:
        """A descriptive slot's value (title/body/recommendation/category + any custom field ride in
        model_extra on this loose carrier), or None if the reviewer dropped it via custom-schemas.
        The one place that knows slots live in model_extra — used by report + PR rendering."""
        return (self.model_extra or {}).get(key)


class ReviewOutput(BaseModel):
    """The carrier for a single harness invocation's output. Top-level shape is fixed
    (`extra="forbid"`); `findings` are the loose `Finding` above, so a custom-schema reviewer's
    reshaped findings round-trip through here and `ReviewResult` unchanged."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ReviewResult(BaseModel):
    """Worker-owned, persisted to reviewers/<reviewer>/<instance>/review.json; holds its status."""

    model_config = ConfigDict(extra="forbid")

    id: str
    reviewer: str
    harness: str
    model: str
    status: ReviewStatus
    started_at: str | None = None
    finished_at: str | None = None
    verdict: Verdict | None = None
    summary: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None


class PooledFinding(Finding):
    """A finding tagged with a stable, run-local id for the dedup harness to reference."""

    id: str


class DuplicateGroup(BaseModel):
    """One dedup decision: the survivor id plus the ids it absorbs."""

    model_config = ConfigDict(extra="forbid")

    survivor: str
    duplicates: list[str] = Field(default_factory=list)


class DuplicateGroups(BaseModel):
    """The dedup harness's output contract: id-groups only, never finding content."""

    model_config = ConfigDict(extra="forbid")

    duplicate_groups: list[DuplicateGroup] = Field(default_factory=list)


class Source(BaseModel):
    """One review that raised a (possibly deduplicated) finding."""

    model_config = ConfigDict(extra="forbid")

    review: str
    severity: Severity
    confidence: float


class MergedFinding(Finding):
    """A survivor finding (kept verbatim) plus its run-local id and provenance after merge."""

    id: str
    sources: list[Source] = Field(default_factory=list)
    agreement: int = 1


class NextStepBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    steps: list[str]


class Coverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contributed: int
    failed: int


class Dedup(BaseModel):
    """The report-level dedup summary. `harness`/`reason`/`warning` carry the failure notice."""

    model_config = ConfigDict(extra="forbid")

    status: DedupState
    harness: str | None = None
    reason: str | None = None
    warning: str | None = None


class UsageBreakdown(BaseModel):
    """Run-total cost, with the dedup call kept separate from the review fan-out."""

    model_config = ConfigDict(extra="forbid")

    reviews: Usage = Field(default_factory=Usage)
    dedup: Usage = Field(default_factory=Usage)
    total: Usage = Field(default_factory=Usage)


class DedupResult(BaseModel):
    """Written to dedup/<instance>/result.json: the harness's grouping decision + its own usage."""

    model_config = ConfigDict(extra="forbid")

    harness: str
    status: DedupState
    started_at: str
    finished_at: str
    groups: list[DuplicateGroup] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    reason: str | None = None
    warning: str | None = None


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    summary: str
    findings: list[MergedFinding] = Field(default_factory=list)
    next_steps: list[NextStepBlock] = Field(default_factory=list)
    coverage: Coverage
    dedup: Dedup
    usage: UsageBreakdown


class ScopeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    base: str | None = None


class Invocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewers: list[str]
    scope: ScopeSpec


class RosterEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    reviewer: str
    harness: str
    model: str
    thinking: str | None = None


class DedupPlan(BaseModel):
    """The dedup harness this run will use. Recorded in run.json only when roster > 1."""

    model_config = ConfigDict(extra="forbid")

    id: str
    harness: str
    model: str
    thinking: str | None = None


class RunManifest(BaseModel):
    """Orchestrator-owned, written to run.json. Run-level only, no per-review status."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    overall: RunState
    invocation: Invocation
    roster: list[RosterEntry]
    dedup: DedupPlan | None = None
    # The dir the run was launched from (its git repo). resume re-runs from here, not the
    # caller's cwd, so a self-collect harness inspects the right repo. (Pydantic serializes Path
    # to the same JSON string, so run.json is unchanged.)
    cwd: Path | None = None
    pid: int | None = None


def review_output_json_schema() -> dict:
    """The default review JSON Schema handed to harnesses — the shape with the built-in findings
    slots. Equivalent to `compose_review_schema(None)`; kept as a named entry point because it's the
    common case and the dedup/test call sites read it directly."""
    return _DefaultReviewOutput.model_json_schema()


def _finding_def(schema: dict) -> dict:
    """The findings-item object in a review schema, resolved by following
    `properties.findings.items.$ref` into `$defs` — robust to whatever pydantic names the def."""
    ref = schema["properties"]["findings"]["items"]["$ref"]
    return schema["$defs"][ref.rsplit("/", 1)[-1]]


def compose_review_schema(custom_schemas: Mapping[str, dict | None] | None = None) -> dict:
    """The per-reviewer review JSON Schema: the default, with each customized slot overridden (a
    JSON Schema fragment) or dropped (None). The skeleton is never touched — only the
    `CUSTOMIZABLE_SLOTS` — so the merge/report/dedup mechanics keep working across a mixed roster.

    A no-op (None/empty) returns the byte-identical default schema, so a reviewer without
    `custom-schemas` is indistinguishable from the pre-feature behaviour.
    """
    schema = deepcopy(review_output_json_schema())
    if not custom_schemas:
        return schema
    finding = _finding_def(schema)
    props: dict = finding["properties"]
    required: list[str] = finding.get("required", [])
    for slot, fragment in custom_schemas.items():
        if fragment is None:  # drop: the reviewer omits this slot entirely
            props.pop(slot, None)
            if slot in required:
                required.remove(slot)
        else:  # override: the fragment fully replaces this slot's subschema
            props[slot] = deepcopy(fragment)
            if slot not in required:
                required.append(slot)
    finding["required"] = required
    return schema


def build_review_validator(schema: dict) -> Callable[[dict], object]:
    """A post-validator derived from a (frozen) review schema.

    For the built-in schema (a reviewer with no `custom-schemas`) this is the strict default
    contract — byte-for-byte the pre-feature validation — so a default reviewer's category enum,
    title length, and stray keys are still caught loudly, notably on the prompt-mode harnesses that
    lean on this post-check.

    For a customized schema it checks what the pipeline depends on — the review shape + the finding
    skeleton (via the loose `ReviewOutput`) — plus that every finding carries the schema's required
    non-skeleton slots. The slots' *inner* shape is enforced by the harness during generation
    (constrained/validated/prompt), not re-checked here (no JSON-Schema engine is bundled).

    Deriving the validator purely from the schema is what makes resume trivially correct: rebuild
    from the frozen schema, never from live `REVIEWER.md`."""
    if schema == review_output_json_schema():
        return _DefaultReviewOutput.model_validate
    required_slots = set(_finding_def(schema).get("required", ())) - set(SKELETON_FIELDS)

    def _validate(payload: dict) -> ReviewOutput:
        review = ReviewOutput.model_validate(payload)  # verdict/summary/skeleton/next_steps
        for finding in payload.get("findings", []):
            if isinstance(finding, dict):
                missing = required_slots - finding.keys()
                if missing:
                    raise ValueError(f"finding missing required field(s): {sorted(missing)}")
        return review

    return _validate


def duplicate_groups_json_schema() -> dict:
    """JSON Schema for the dedup harness's id-group output."""
    return DuplicateGroups.model_json_schema()


def make_strict_schema(base: dict) -> dict:
    """Return an OpenAI strict-mode copy of a JSON Schema for codex's constrained decoding.

    Strict mode requires every object to list *all* its properties in `required` and to set
    `additionalProperties: false`. pydantic omits fields with defaults from `required`, which
    codex rejects — so mark every property required, recursively, on a copy (the lenient base
    is reused as-is by validate-and-reprompt harnesses).
    """
    schema = deepcopy(base)
    _make_strict(schema)
    return schema


def _make_strict(node: object) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            node["required"] = list(node["properties"].keys())
            node["additionalProperties"] = False
        for value in node.values():
            _make_strict(value)
    elif isinstance(node, list):
        for item in node:
            _make_strict(item)
