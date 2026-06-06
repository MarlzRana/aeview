"""Owns the on-disk run directory: ~/.aeview/runs/<uuid>/.

aeview is the sole writer. The orchestrator owns run.json; each worker is the sole writer
of its reviewers/<reviewer>/<instance>/review.json. Inputs are grouped by phase: the shared
diff in bundle/, then per-reviewer dirs that hold the shared prompt.md plus one subdir per
harness instance (the review). All writes are atomic (tmp file -> os.replace) so a SIGKILL
mid-write can never leave a half-written JSON that a reader would trust.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .bundle import Bundle
from .config import runs_dir
from .schema import Report, ReviewResult, RunManifest


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    return str(uuid.uuid4())


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _self_collect_md(bundle: Bundle, full_diff: Path) -> str:
    inspect = "\n".join(f"- `{cmd}`" for cmd in bundle.inspect) or "- (read the diff file)"
    return (
        f"# Self-collect bundle\n\n"
        f"Scope: {bundle.scope.type} (base {bundle.scope.base})\n"
        f"Diff size: {bundle.diff_bytes} bytes (over the inline threshold)\n\n"
        f"Full diff: {full_diff}\n\n"
        f"Inspect read-only:\n{inspect}\n\n"
        f"## Summary\n\n{bundle.summary}\n"
    )


class RunStore:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.dir = runs_dir() / run_id
        self.bundle_dir = self.dir / "bundle"
        self.reviewers_dir = self.dir / "reviewers"

    @classmethod
    def create(cls, run_id: str) -> RunStore:
        store = cls(run_id)
        for d in (store.dir, store.bundle_dir, store.reviewers_dir):
            d.mkdir(parents=True, exist_ok=True)
        return store

    # --- run.json (orchestrator-owned) ---
    def write_manifest(self, manifest: RunManifest) -> None:
        _atomic_write(self.dir / "run.json", manifest.model_dump_json(indent=2))

    def read_manifest(self) -> RunManifest:
        return RunManifest.model_validate_json((self.dir / "run.json").read_text("utf-8"))

    # --- bundle/ ---
    def write_bundle(self, bundle: Bundle) -> Path | None:
        """Persist bundle artifacts. Returns the full-diff path in self-collect mode."""
        _atomic_write(self.bundle_dir / "bundle.json", json.dumps(bundle.manifest(), indent=2))
        if bundle.is_inline:
            _atomic_write(self.bundle_dir / "inline_bundle.diff", bundle.diff)
            return None
        full = self.bundle_dir / "self_collect.diff"
        _atomic_write(full, bundle.diff)
        _atomic_write(self.bundle_dir / "self_collect_bundle.md", _self_collect_md(bundle, full))
        return full

    # --- reviewers/<reviewer>/ (prompt shared across that reviewer's harness instances) ---
    def write_prompt(self, reviewer: str, prompt: str) -> None:
        reviewer_dir = self.reviewers_dir / reviewer
        reviewer_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(reviewer_dir / "prompt.md", prompt)

    # --- reviewers/<reviewer>/<instance>/ (one review = reviewer x harness instance) ---
    def _review_dir(self, reviewer: str, review_id: str) -> Path:
        # review_id is "<reviewer>__<instance>"; the instance is the on-disk subdir name.
        instance = review_id.removeprefix(f"{reviewer}__")
        return self.reviewers_dir / reviewer / instance

    def review_path(self, reviewer: str, review_id: str) -> Path:
        return self._review_dir(reviewer, review_id) / "review.json"

    def write_review(self, result: ReviewResult) -> None:
        review_dir = self._review_dir(result.reviewer, result.id)
        review_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(review_dir / "review.json", result.model_dump_json(indent=2))

    def log_path(self, reviewer: str, review_id: str) -> Path:
        review_dir = self._review_dir(reviewer, review_id)
        review_dir.mkdir(parents=True, exist_ok=True)
        return review_dir / "review.log"

    def read_reviews(self) -> list[ReviewResult]:
        results: list[ReviewResult] = []
        for path in sorted(self.reviewers_dir.glob("*/*/review.json")):
            results.append(ReviewResult.model_validate_json(path.read_text("utf-8")))
        return results

    # --- report.json (written last) ---
    def write_report(self, report: Report) -> None:
        _atomic_write(self.dir / "report.json", report.model_dump_json(indent=2))

    def read_report(self) -> Report:
        return Report.model_validate_json((self.dir / "report.json").read_text("utf-8"))
