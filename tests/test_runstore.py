from __future__ import annotations

from aeview import fanout
from aeview.harness.base import HarnessOutput
from aeview.runstore import RunStore, new_run_id
from aeview.schema import ReviewOutput, ReviewResult, RosterEntry, Usage


def _result(review_id: str, reviewer: str) -> ReviewResult:
    return ReviewResult(
        id=review_id, reviewer=reviewer, harness="claude-code", model="opus", status="done"
    )


# --- path derivation (pure, independent of write_review) -------------------------------


def test_review_and_log_paths_derive_instance_from_id(aeview_home):
    store = RunStore(new_run_id())
    review = store.review_path("python", "python__codex-gpt-5.5")
    assert review == store.reviewers_dir / "python" / "codex-gpt-5.5" / "review.json"
    assert store.log_path("python", "python__codex-gpt-5.5") == review.with_name("review.log")


def test_instance_segment_keeps_dashes_and_does_not_split_on_them(aeview_home):
    # The instance id can contain '-' (model names, escalated thinking); removeprefix must
    # strip only the "<reviewer>__" prefix, not split on '-'.
    store = RunStore(new_run_id())
    path = store.review_path("default", "default__claude-code-opus-4-8-high")
    assert path.parent.name == "claude-code-opus-4-8-high"


# --- read_reviews ordering -------------------------------------------------------------


def test_read_reviews_orders_by_id_not_glob_path(aeview_home):
    # 'test' / 'test2': a path sort ("/" = 0x2F) orders [test, test2], but the canonical id
    # sort ("__" = 0x5F) orders [test2, test]. read_reviews must follow the id, not the path.
    store = RunStore.create(new_run_id())
    store.write_review(_result("test__claude-code-opus", "test"))
    store.write_review(_result("test2__claude-code-opus", "test2"))

    got = [r.id for r in store.read_reviews()]
    assert got == ["test2__claude-code-opus", "test__claude-code-opus"]


# --- multi-instance layout through the fan-out -----------------------------------------


class _OkAdapter:
    """Writes its log (as the real adapters do) and returns an approve review."""

    async def run(self, prompt, model, cwd, log_path, thinking=None):
        log_path.write_text("stub log", encoding="utf-8")
        return HarnessOutput(
            review=ReviewOutput(verdict="approve", summary="ok", findings=[], next_steps=[]),
            usage=Usage(),
            raw="{}",
        )


async def test_one_reviewer_two_harnesses_writes_distinct_instance_dirs(aeview_home, monkeypatch):
    monkeypatch.setattr(fanout, "get_adapter", lambda h: _OkAdapter())
    store = RunStore.create(new_run_id())
    store.write_prompt("tests", "SHARED PROMPT")
    roster = [
        RosterEntry(id="tests__codex-gpt-5.5", reviewer="tests", harness="codex", model="gpt-5.5"),
        RosterEntry(
            id="tests__claude-code-opus", reviewer="tests", harness="claude-code", model="opus"
        ),
    ]

    results = await fanout.fan_out(store, roster, {"tests": "SHARED PROMPT"}, aeview_home)

    assert {r.status for r in results} == {"done"}
    reviewer_dir = store.reviewers_dir / "tests"
    # One shared prompt, two instance subdirs, each self-contained (review.json + review.log).
    assert (reviewer_dir / "prompt.md").read_text() == "SHARED PROMPT"
    for instance in ("codex-gpt-5.5", "claude-code-opus"):
        assert (reviewer_dir / instance / "review.json").exists()
        assert (reviewer_dir / instance / "review.log").exists()
    # read_reviews aggregates both instances of the one reviewer.
    assert [r.id for r in store.read_reviews()] == [
        "tests__claude-code-opus",
        "tests__codex-gpt-5.5",
    ]
