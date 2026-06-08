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

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal["bug", "security", "regression", "test_gap", "maintainability"]
Verdict = Literal["approve", "needs-attention"]
ReviewStatus = Literal["pending", "running", "done", "failed"]
RunState = Literal["running", "done", "failed", "interrupted"]
DedupState = Literal["ok", "skipped", "failed"]


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    line_start: int = Field(ge=0)
    line_end: int = Field(ge=0)


class Finding(BaseModel):
    """A single issue as emitted by a reviewer (no provenance yet)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=140)
    body: str
    severity: Severity
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    location: Location
    recommendation: str


class ReviewOutput(BaseModel):
    """The structured output contract for a single harness invocation."""

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
    # The dir the run was launched from (its git repo). resume/the detached worker re-run from
    # here, not the caller's cwd, so a self-collect harness inspects the right repo.
    cwd: str | None = None
    pid: int | None = None
    pgid: int | None = None


def review_output_json_schema() -> dict:
    """JSON Schema handed to harnesses that support structured output."""
    return ReviewOutput.model_json_schema()


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
