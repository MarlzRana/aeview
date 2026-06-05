"""Owns the on-disk run directory: ~/.aeview/runs/<uuid>/.

aeview is the sole writer. The orchestrator owns run.json; each worker is the sole
writer of its reviews/<id>.json. All writes are atomic (tmp file -> os.replace) so a
SIGKILL mid-write can never leave a half-written JSON that a reader would trust.
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


class RunStore:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.dir = runs_dir() / run_id
        self.bundle_dir = self.dir / "bundle"
        self.prompt_dir = self.bundle_dir / "prompt"
        self.reviews_dir = self.dir / "reviews"
        self.logs_dir = self.dir / "logs"

    @classmethod
    def create(cls, run_id: str) -> RunStore:
        store = cls(run_id)
        for d in (store.dir, store.bundle_dir, store.prompt_dir, store.reviews_dir, store.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
        return store

    # --- run.json (orchestrator-owned) ---
    def write_manifest(self, manifest: RunManifest) -> None:
        _atomic_write(self.dir / "run.json", manifest.model_dump_json(indent=2))

    def read_manifest(self) -> RunManifest:
        return RunManifest.model_validate_json((self.dir / "run.json").read_text("utf-8"))

    # --- bundle/ ---
    def write_bundle(self, bundle: Bundle) -> None:
        _atomic_write(self.bundle_dir / "bundle.json", json.dumps(bundle.manifest(), indent=2))
        _atomic_write(self.bundle_dir / "inline_bundle.diff", bundle.diff)

    def write_prompt(self, reviewer: str, prompt: str) -> None:
        _atomic_write(self.prompt_dir / f"{reviewer}.md", prompt)

    # --- reviews/<id>.json (worker-owned) ---
    def write_review(self, result: ReviewResult) -> None:
        _atomic_write(self.reviews_dir / f"{result.id}.json", result.model_dump_json(indent=2))

    def log_path(self, review_id: str) -> Path:
        return self.logs_dir / f"{review_id}.log"

    def read_reviews(self) -> list[ReviewResult]:
        results: list[ReviewResult] = []
        for path in sorted(self.reviews_dir.glob("*.json")):
            results.append(ReviewResult.model_validate_json(path.read_text("utf-8")))
        return results

    # --- report.json (written last) ---
    def write_report(self, report: Report) -> None:
        _atomic_write(self.dir / "report.json", report.model_dump_json(indent=2))

    def read_report(self) -> Report:
        return Report.model_validate_json((self.dir / "report.json").read_text("utf-8"))
