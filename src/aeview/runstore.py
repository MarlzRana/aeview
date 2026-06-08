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
from .schema import DedupResult, PooledFinding, Report, ReviewResult, RunManifest, RunState

# A run is non-terminal only while 'running'; every other state is final.
_NON_TERMINAL: RunState = "running"

# Microsecond resolution so back-to-back/parallel runs get distinct, correctly-orderable stamps
# (a whole-second stamp made same-second runs tie, leaving `latest`/prune to an arbitrary
# UUID order). _LEGACY_TS_FMT parses runs written before micro resolution.
_RUN_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
_LEGACY_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
_DT_MIN = datetime.min.replace(tzinfo=UTC)


def now_iso() -> str:
    return datetime.now(UTC).strftime(_RUN_TS_FMT)


def _parse_ts(ts: str) -> datetime | None:
    for fmt in (_RUN_TS_FMT, _LEGACY_TS_FMT):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _iter_run_dirs() -> Iterator[tuple[Path, RunManifest]]:
    """Yield (run dir, manifest) for each readable run, in filesystem order.

    The enumerated *directory* is a run's authoritative identity — callers act on this exact
    path, never on a path rebuilt from the manifest's self-declared `run_id` (a corrupt or
    hostile run.json could point at another dir, or an absolute path, and redirect a delete
    outside ~/.aeview/runs). The parsed manifest's `run_id` is normalized to the dir name so
    every downstream reader (`list`/`status`/`latest_run_id`) is dir-authoritative too. A dir
    without a readable/valid run.json is skipped, not an error: a run killed before its first
    manifest write, or a hand-corrupted dir, shouldn't break `list`/`status`/prune.
    """
    root = runs_dir()
    if not root.is_dir():
        return
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            # read + parse in one guard: OSError (missing/unreadable run.json) and ValueError —
            # which covers both bad JSON and invalid UTF-8 (UnicodeDecodeError is a ValueError).
            manifest = RunManifest.model_validate_json((child / "run.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        manifest.run_id = child.name  # the directory wins over the self-declared field
        yield child, manifest


def _runs_newest_first() -> list[tuple[Path, RunManifest]]:
    # Order by the parsed timestamp so back-to-back runs sort by true creation time (micro
    # resolution); an unparseable stamp sorts oldest. run_id is a final deterministic tiebreak
    # for the astronomically-rare identical-microsecond case (filesystem order is not stable).
    return sorted(
        _iter_run_dirs(),
        key=lambda e: (_parse_ts(e[1].created_at) or _DT_MIN, e[1].run_id),
        reverse=True,
    )


def list_manifests() -> list[RunManifest]:
    """Every run's manifest, newest-first (deterministic tie-break on run id)."""
    return [manifest for _, manifest in _runs_newest_first()]


def latest_run_id() -> str | None:
    runs = _runs_newest_first()
    return runs[0][1].run_id if runs else None


def prune_runs(retention: Retention) -> list[str]:
    """Delete a terminal run only if it's BOTH outside the newest keepLast AND older than ttlDays;
    return the deleted ids. keepLast is a guaranteed floor — the newest keepLast terminal runs are
    always kept regardless of age — and ttlDays only evicts runs beyond that floor, so an
    infrequent user never loses their last keepLast runs to age.

    Deletes the *enumerated* run dir, never a path rebuilt from the manifest's run_id, so a
    corrupt run.json can't redirect the delete outside ~/.aeview/runs. Only terminal runs are
    candidates — a still-'running' run here is genuinely alive (crashed 'running' runs were first
    reconciled to 'interrupted' by reconcile_interrupted, so they become prunable). Protection
    counts terminal runs only, so a live run can't shrink the floor for real history. Best-effort:
    a deletion error is skipped rather than aborting the caller (this runs at the start of `run`).
    """
    terminal = [(child, m) for child, m in _runs_newest_first() if m.overall != _NON_TERMINAL]
    protected = {child for child, _ in terminal[: retention.keep_last]}
    cutoff = datetime.now(UTC) - timedelta(days=retention.ttl_days)
    removed: list[str] = []
    for child, manifest in terminal:
        ts = _parse_ts(manifest.created_at)
        too_old = ts is not None and ts < cutoff
        if child in protected or not too_old:  # keep within the keepLast floor OR within ttlDays
            continue
        # A concurrent `resume` may have taken this (old, terminal) run live since the snapshot;
        # re-read and skip if it's now running, so we never rmtree a run being actively resumed.
        try:
            if effective_overall(RunStore(child.name).read_manifest()) == _NON_TERMINAL:
                continue
        except (OSError, ValueError):
            continue
        try:
            shutil.rmtree(child)
        except OSError:
            continue
        removed.append(manifest.run_id)
    return removed


def pid_alive(pid: int | None) -> bool:
    """Whether a recorded run pid is still alive (os.kill(pid, 0)). No recorded pid (or a pid
    that's gone) reads as not-alive — a crashed or pre-liveness run, safe to reconcile. A
    non-positive pid is never alive: os.kill(0, 0) would signal the *caller's* process group
    (always succeeds), so a corrupt/empty lock holder (parsed as 0) must not read as live."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # the pid exists but is owned by another uid — still alive
    return True


def _is_crashed(manifest: RunManifest) -> bool:
    """A run that's still marked 'running' but whose recorded pid is dead — i.e. interrupted."""
    return manifest.overall == _NON_TERMINAL and not pid_alive(manifest.pid)


def effective_overall(manifest: RunManifest) -> RunState:
    """The run's state accounting for liveness: a crashed 'running' run is reported as
    'interrupted'. Read-only — display callers (status/list/--wait) use this; prune/resume make it
    durable via reconcile_interrupted or their own terminal write."""
    return "interrupted" if _is_crashed(manifest) else manifest.overall


def reconcile_interrupted() -> list[str]:
    """Persist the liveness verdict for crashed runs: a 'running' run whose recorded pid is dead is
    rewritten to 'interrupted' (terminal — prune can then collect it, resume can recover it).
    Returns the reconciled run-ids. Best-effort: a write error is skipped. Runs at the start of
    `run` (before prune) so a Ctrl-C'd/killed run doesn't linger 'running' or leak forever."""
    reconciled: list[str] = []
    for child, manifest in _iter_run_dirs():
        if not _is_crashed(manifest):
            continue
        # Re-read right before clobbering: the worker writes its terminal manifest *then* exits,
        # so a now-dead pid means a 'done'/'failed' may already be on disk that our cached read
        # (taken while it was still 'running') missed; and a concurrent resume may have taken it
        # over (fresh live pid). Either way, don't overwrite a finished/active run.
        store = RunStore(child.name)
        try:
            fresh = store.read_manifest()
        except (OSError, ValueError):
            continue
        if not _is_crashed(fresh):
            continue
        fresh.run_id = child.name
        fresh.overall = "interrupted"
        fresh.finished_at = now_iso()
        try:
            store.write_manifest(fresh)
        except OSError:
            continue
        reconciled.append(child.name)
    return reconciled


def pool_to_json(pool: list[PooledFinding]) -> str:
    """The pool as a pretty JSON array (one finding per line) — the exact bytes the dedup
    harness sees, reused for both the composed prompt and the persisted input.json."""
    if not pool:
        return "[]"
    return "[\n" + ",\n".join(f.model_dump_json() for f in pool) + "\n]"


def new_run_id() -> str:
    return str(uuid.uuid4())


def atomic_write_text(path: Path, text: str) -> None:
    """Publish all-or-nothing (tmp -> os.replace): a reader never sees a half-written file, and a
    SIGKILL mid-write leaves either the old file or the complete new one — never a truncated JSON
    a reader would trust. The package's single atomic-write primitive (reused by the CLI)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)  # don't leave a partial .tmp behind on a failed write
        raise


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
        atomic_write_text(self.dir / "run.json", manifest.model_dump_json(indent=2))

    def read_manifest(self) -> RunManifest:
        return RunManifest.model_validate_json((self.dir / "run.json").read_text("utf-8"))

    # --- bundle/ ---
    def write_bundle(self, bundle: Bundle) -> Path | None:
        """Persist bundle artifacts. Returns the full-diff path in self-collect mode."""
        atomic_write_text(self.bundle_dir / "bundle.json", json.dumps(bundle.manifest(), indent=2))
        if bundle.is_inline:
            atomic_write_text(self.bundle_dir / "inline_bundle.diff", bundle.diff)
            return None
        full = self.bundle_dir / "self_collect.diff"
        atomic_write_text(full, bundle.diff)
        summary_md = _self_collect_md(bundle, full)
        atomic_write_text(self.bundle_dir / "self_collect_bundle.md", summary_md)
        return full

    # --- reviewers/<reviewer>/ (prompt shared across that reviewer's harness instances) ---
    def write_prompt(self, reviewer: str, prompt: str) -> None:
        reviewer_dir = self.reviewers_dir / reviewer
        reviewer_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(reviewer_dir / "prompt.md", prompt)

    def read_prompt(self, reviewer: str) -> str:
        # resume reuses the prompt frozen at run start (never re-composes), so a resumed review
        # sees byte-identical input to the original.
        return (self.reviewers_dir / reviewer / "prompt.md").read_text("utf-8")

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
        atomic_write_text(review_dir / "review.json", result.model_dump_json(indent=2))

    def log_path(self, reviewer: str, review_id: str) -> Path:
        # A pure path accessor: the review dir already exists because the worker writes the
        # `running` ReviewResult (write_review) before the harness logs anything.
        return self._review_dir(reviewer, review_id) / "review.log"

    def read_reviews(self) -> list[ReviewResult]:
        results: list[ReviewResult] = []
        for path in self.reviewers_dir.glob("*/*/review.json"):
            # Skip a corrupt/incompatible review.json rather than crash every caller (status,
            # resume): mirrors list_manifests' run.json tolerance. OSError + ValueError covers an
            # unreadable file, bad JSON, invalid UTF-8, and (extra="forbid") cross-version drift.
            try:
                results.append(ReviewResult.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValueError):
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
        atomic_write_text(self._dedup_dir(instance_id) / "prompt.md", prompt)

    def write_dedup_input(self, instance_id: str, pool: list[PooledFinding]) -> None:
        atomic_write_text(self._dedup_dir(instance_id) / "input.json", pool_to_json(pool) + "\n")

    def write_dedup_result(self, instance_id: str, result: DedupResult) -> None:
        path = self._dedup_dir(instance_id) / "result.json"
        atomic_write_text(path, result.model_dump_json(indent=2))

    def dedup_log_path(self, instance_id: str) -> Path:
        return self._dedup_dir(instance_id) / "dedup.log"

    # --- report.json (written last) ---
    def write_report(self, report: Report) -> None:
        atomic_write_text(self.dir / "report.json", report.model_dump_json(indent=2))

    def read_report(self) -> Report:
        return Report.model_validate_json((self.dir / "report.json").read_text("utf-8"))

    def clear_report(self) -> None:
        # resume drops the stale report before re-running, so a crash mid-resume can't leave a
        # readable-but-outdated verdict for result / status --wait.
        (self.dir / "report.json").unlink(missing_ok=True)
