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


class Source(BaseModel):
    """One review that raised a (possibly deduplicated) finding."""

    model_config = ConfigDict(extra="forbid")

    review: str
    severity: Severity
    confidence: float


class MergedFinding(Finding):
    """A survivor finding plus provenance after merge."""

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
    model_config = ConfigDict(extra="forbid")

    status: DedupState


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    summary: str
    findings: list[MergedFinding] = Field(default_factory=list)
    next_steps: list[NextStepBlock] = Field(default_factory=list)
    coverage: Coverage
    dedup: Dedup
    usage: Usage


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
    pid: int | None = None
    pgid: int | None = None


def review_output_json_schema() -> dict:
    """JSON Schema handed to harnesses that support structured output."""
    return ReviewOutput.model_json_schema()


def strict_review_output_schema() -> dict:
    """OpenAI strict-mode schema for codex's constrained decoding.

    Strict mode requires every object to list *all* its properties in `required` and to set
    `additionalProperties: false`. pydantic omits fields with defaults (findings/next_steps)
    from `required`, which codex rejects — so mark every property required, recursively.
    """
    schema = ReviewOutput.model_json_schema()
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
