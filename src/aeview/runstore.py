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
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .bundle import Bundle
from .config import Retention, runs_dir
from .schema import DedupResult, PooledFinding, Report, ReviewResult, RunManifest

_RUN_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    return datetime.now(UTC).strftime(_RUN_TS_FMT)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, _RUN_TS_FMT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _iter_run_dirs() -> Iterator[tuple[Path, RunManifest]]:
    """Yield (run dir, manifest) for each readable run, in filesystem order.

    The enumerated *directory* is a run's authoritative identity — callers act on this exact
    path, never on a path rebuilt from the manifest's self-declared `run_id` (a corrupt or
    hostile run.json could point at another dir, or an absolute path, and redirect a delete
    outside ~/.aeview/runs). A dir without a readable/valid run.json is skipped, not an error:
    a run killed before its first manifest write, or a hand-corrupted dir, shouldn't break
    `list`/`status`/prune.
    """
    root = runs_dir()
    if not root.is_dir():
        return
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            text = (child / "run.json").read_text("utf-8")
        except OSError:
            continue
        try:
            yield child, RunManifest.model_validate_json(text)
        except ValueError:
            continue


def _runs_newest_first() -> list[tuple[Path, RunManifest]]:
    # created_at is ISO Z (lexical == chronological); tie-break equal second-resolution stamps
    # by run id so ordering is deterministic (filesystem iteration order is not). Sub-second
    # creation order is not tracked — same-second runs order by id, not by true arrival.
    return sorted(_iter_run_dirs(), key=lambda e: (e[1].created_at, e[1].run_id), reverse=True)


def list_manifests() -> list[RunManifest]:
    """Every run's manifest, newest-first (deterministic tie-break on run id)."""
    return [manifest for _, manifest in _runs_newest_first()]


def latest_run_id() -> str | None:
    runs = _runs_newest_first()
    return runs[0][1].run_id if runs else None


def prune_runs(retention: Retention) -> list[str]:
    """Delete terminal runs outside the newest keepLast OR older than ttlDays; return their ids.

    Deletes the *enumerated* run dir, never a path rebuilt from the manifest's run_id, so a
    corrupt run.json can't redirect the delete outside ~/.aeview/runs. Never deletes a
    non-terminal ('running') run — it may still be writing, and I6a has no liveness check to
    tell a live run from a crashed one. Best-effort: a deletion error is skipped rather than
    aborting the caller (this runs at the start of every `run`).
    """
    runs = _runs_newest_first()
    protected = {child for child, _ in runs[: retention.keep_last]}
    cutoff = datetime.now(UTC) - timedelta(days=retention.ttl_days)
    removed: list[str] = []
    for child, manifest in runs:
        if manifest.overall == "running":
            continue
        ts = _parse_ts(manifest.created_at)
        too_old = ts is not None and ts < cutoff
        if child in protected and not too_old:
            continue
        try:
            shutil.rmtree(child)
        except OSError:
            continue
        removed.append(manifest.run_id)
    return removed


def pool_to_json(pool: list[PooledFinding]) -> str:
    """The pool as a pretty JSON array (one finding per line) — the exact bytes the dedup
    harness sees, reused for both the composed prompt and the persisted input.json."""
    if not pool:
        return "[]"
    return "[\n" + ",\n".join(f.model_dump_json() for f in pool) + "\n]"


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
        # A pure path accessor: the review dir already exists because the worker writes the
        # `running` ReviewResult (write_review) before the harness logs anything.
        return self._review_dir(reviewer, review_id) / "review.log"

    def read_reviews(self) -> list[ReviewResult]:
        results: list[ReviewResult] = []
        for path in self.reviewers_dir.glob("*/*/review.json"):
            # Skip a corrupt/incompatible review.json rather than crash every caller (status,
            # resume): mirrors list_manifests' run.json tolerance. extra="forbid" means even
            # cross-version schema drift in a stray file would otherwise raise here.
            try:
                text = path.read_text("utf-8")
            except OSError:
                continue
            try:
                results.append(ReviewResult.model_validate_json(text))
            except ValueError:
                continue
        # Sort by the canonical review id, not the glob path: "<reviewer>/<instance>" orders on
        # "/" while the id orders on "__", so a path sort can reorder prefix-named reviewers.
        results.sort(key=lambda r: r.id)
        return results

    # --- dedup/<instance>/ (the one dedup call; absent when dedup is skipped) ---
    def _dedup_dir(self, instance_id: str) -> Path:
        d = self.dir / "dedup" / instance_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_dedup_prompt(self, instance_id: str, prompt: str) -> None:
        _atomic_write(self._dedup_dir(instance_id) / "prompt.md", prompt)

    def write_dedup_input(self, instance_id: str, pool: list[PooledFinding]) -> None:
        _atomic_write(self._dedup_dir(instance_id) / "input.json", pool_to_json(pool) + "\n")

    def write_dedup_result(self, instance_id: str, result: DedupResult) -> None:
        path = self._dedup_dir(instance_id) / "result.json"
        _atomic_write(path, result.model_dump_json(indent=2))

    def dedup_log_path(self, instance_id: str) -> Path:
        return self._dedup_dir(instance_id) / "dedup.log"

    # --- report.json (written last) ---
    def write_report(self, report: Report) -> None:
        _atomic_write(self.dir / "report.json", report.model_dump_json(indent=2))

    def read_report(self) -> Report:
        return Report.model_validate_json((self.dir / "report.json").read_text("utf-8"))
