from __future__ import annotations

from aeview import merge as merge_mod
from aeview.config import HarnessInstance, Settings
from aeview.dedup import DedupOutcome
from aeview.merge import merge_reviews
from aeview.runstore import RunStore, new_run_id
from aeview.schema import (
    DuplicateGroup,
    Finding,
    Location,
    ReviewResult,
    Severity,
    Usage,
    Verdict,
)


def _finding(title: str, severity: Severity, *, file="a.py", line=1, conf=0.5) -> Finding:
    return Finding(
        title=title,
        body="b",
        severity=severity,
        category="bug",
        confidence=conf,
        location=Location(file=file, line_start=line, line_end=line),
        recommendation="fix",
    )


def _done(rid: str, findings: list[Finding], verdict: Verdict, reviewer="default") -> ReviewResult:
    return ReviewResult(
        id=rid,
        reviewer=reviewer,
        harness="claude-code",
        model="sonnet",
        status="done",
        verdict=verdict,
        summary="s",
        findings=findings,
        next_steps=["step"],
        usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.01),
    )


def _settings() -> Settings:
    return Settings(
        deduplication_harness=HarnessInstance(harness="claude-code", model="opus", thinking="high")
    )


async def _merge(results, aeview_home, settings=None):
    store = RunStore.create(new_run_id())
    return await merge_reviews(results, settings or _settings(), store, aeview_home)


# --- single review: dedup skipped, passthrough -----------------------------------------


async def test_single_review_passthrough_has_provenance(aeview_home):
    review = _done("default__claude-code-sonnet", [_finding("x", "high")], "needs-attention")
    report = await _merge([review], aeview_home)
    assert report.verdict == "needs-attention"
    assert report.dedup.status == "skipped"
    assert report.coverage.contributed == 1
    assert report.findings[0].agreement == 1
    assert report.findings[0].id == "f1"
    assert report.findings[0].sources[0].review == "default__claude-code-sonnet"
    assert report.usage.reviews.cost_usd == 0.01
    assert report.usage.total.cost_usd == 0.01  # no dedup ran


async def test_approve_when_no_findings(aeview_home):
    report = await _merge([_done("r", [], "approve")], aeview_home)
    assert report.verdict == "approve"
    assert report.findings == []


async def test_findings_sorted_by_severity(aeview_home):
    review = _done("r", [_finding("low", "low"), _finding("crit", "critical")], "needs-attention")
    report = await _merge([review], aeview_home)
    assert [f.severity for f in report.findings] == ["critical", "low"]


async def test_failed_review_counted_in_coverage(aeview_home):
    failed = ReviewResult(
        id="r2", reviewer="default", harness="claude-code", model="sonnet", status="failed",
        error="boom",
    )
    review = _done("r1", [_finding("x", "high")], "needs-attention")
    report = await _merge([review, failed], aeview_home)
    assert report.coverage.contributed == 1
    assert report.coverage.failed == 1
    assert "1 failed" in report.summary


async def test_all_reviews_failed_forces_needs_attention(aeview_home):
    # The deferred bug: a run where every review failed must not report a green approve.
    failed = ReviewResult(
        id="r", reviewer="default", harness="claude-code", model="sonnet", status="failed",
        error="boom",
    )
    report = await _merge([failed], aeview_home)
    assert report.coverage.contributed == 0
    assert report.verdict == "needs-attention"


# --- multi-review: dedup applied -------------------------------------------------------


def _two_reviews() -> list[ReviewResult]:
    return [
        _done("a__claude-code-sonnet", [_finding("null deref", "high")], "needs-attention", "a"),
        _done(
            "b__claude-code-opus",
            [_finding("possible null deref", "critical", conf=0.9)],
            "needs-attention",
            "b",
        ),
    ]


async def test_dedup_groups_collapse_to_one_survivor(aeview_home, monkeypatch):
    # f1 (a, high) and f2 (b, critical) are judged the same issue; survivor f2 kept verbatim.
    async def fake_dedup(pool, instance, store, cwd, timeout=600.0):
        return DedupOutcome(
            "ok", [DuplicateGroup(survivor="f2", duplicates=["f1"])], Usage(cost_usd=0.02), "dx"
        )

    monkeypatch.setattr(merge_mod, "run_dedup", fake_dedup)
    report = await _merge(_two_reviews(), aeview_home)

    assert report.dedup.status == "ok"
    assert report.dedup.harness == "dx"
    assert len(report.findings) == 1
    survivor = report.findings[0]
    assert survivor.id == "f2"
    assert survivor.severity == "critical"  # b's own value, verbatim — not a max
    assert survivor.agreement == 2
    assert {s.review for s in survivor.sources} == {"a__claude-code-sonnet", "b__claude-code-opus"}
    assert "1 corroborated" in report.summary
    assert report.usage.dedup.cost_usd == 0.02
    assert report.usage.total.cost_usd == 0.04  # 2 reviews x 0.01 + dedup 0.02


async def test_dedup_failure_emits_raw_union_with_notice(aeview_home, monkeypatch):
    async def fake_dedup(pool, instance, store, cwd, timeout=600.0):
        return DedupOutcome(
            "failed", [], Usage(), "dx", reason="harness timed out after 600s", warning="W"
        )

    monkeypatch.setattr(merge_mod, "run_dedup", fake_dedup)
    report = await _merge(_two_reviews(), aeview_home)

    assert report.dedup.status == "failed"
    assert report.dedup.reason == "harness timed out after 600s"
    assert len(report.findings) == 2  # raw union — duplicates NOT removed
    assert all(f.agreement == 1 for f in report.findings)


async def test_dedup_skipped_when_fewer_than_two_findings(aeview_home, monkeypatch):
    # Two reviews but only one finding total -> nothing could be a duplicate -> skip the
    # billed harness call entirely (run_dedup must not be invoked).
    called = False

    async def fake_dedup(pool, instance, store, cwd, timeout=600.0):
        nonlocal called
        called = True
        return DedupOutcome("ok", [], Usage(), "dx")

    monkeypatch.setattr(merge_mod, "run_dedup", fake_dedup)
    reviews = [
        _done("a__h", [_finding("only", "high")], "needs-attention", "a"),
        _done("b__h", [], "approve", "b"),
    ]
    report = await _merge(reviews, aeview_home)
    assert report.dedup.status == "skipped"
    assert called is False  # gate skipped the harness call
    assert len(report.findings) == 1


async def test_dedup_unconfigured_with_multiple_reviews_fails_loud(aeview_home):
    settings = Settings(deduplication_harness=None)
    report = await _merge(_two_reviews(), aeview_home, settings)
    assert report.dedup.status == "failed"
    assert "no deduplicationHarness" in (report.dedup.reason or "")
    assert len(report.findings) == 2  # raw union


async def test_invalid_survivor_falls_back_to_strongest(aeview_home, monkeypatch):
    # Harness nominates an id that isn't in the group; aeview falls back to severity->conf->id.
    async def fake_dedup(pool, instance, store, cwd, timeout=600.0):
        return DedupOutcome(
            "ok", [DuplicateGroup(survivor="f99", duplicates=["f1", "f2"])], Usage(), "dx"
        )

    monkeypatch.setattr(merge_mod, "run_dedup", fake_dedup)
    report = await _merge(_two_reviews(), aeview_home)
    assert len(report.findings) == 1
    assert report.findings[0].id == "f2"  # critical beats high
    assert report.findings[0].agreement == 2


def _three_reviews() -> list[ReviewResult]:
    # pool: f1=dup-x(a,high), f2=unique-y(a,medium), f3=dup-x again(b,critical)
    a_findings = [_finding("dup-x", "high"), _finding("unique-y", "medium")]
    return [
        _done("a__h", a_findings, "needs-attention", "a"),
        _done("b__h", [_finding("dup-x again", "critical")], "needs-attention", "b"),
    ]


async def test_ungrouped_findings_survive_as_singletons(aeview_home, monkeypatch):
    # A group covers only f1+f3; f2 is never mentioned and must survive on its own (no loss).
    async def fake_dedup(pool, instance, store, cwd, timeout=600.0):
        return DedupOutcome("ok", [DuplicateGroup(survivor="f3", duplicates=["f1"])], Usage(), "dx")

    monkeypatch.setattr(merge_mod, "run_dedup", fake_dedup)
    report = await _merge(_three_reviews(), aeview_home)

    by_id = {f.id: f for f in report.findings}
    assert set(by_id) == {"f3", "f2"}  # f1 absorbed into f3; f2 kept as a singleton
    assert by_id["f3"].agreement == 2
    assert by_id["f2"].agreement == 1


async def test_hostile_groups_no_loss_no_double_count(aeview_home, monkeypatch):
    # Overlapping groups, a repeated id, and an unknown id — every real finding appears once.
    async def fake_dedup(pool, instance, store, cwd, timeout=600.0):
        return DedupOutcome(
            "ok",
            [
                DuplicateGroup(survivor="f1", duplicates=["f3", "f3", "f404"]),  # repeat + unknown
                DuplicateGroup(survivor="f3", duplicates=["f1"]),  # already consumed -> dropped
            ],
            Usage(),
            "dx",
        )

    monkeypatch.setattr(merge_mod, "run_dedup", fake_dedup)
    report = await _merge(_three_reviews(), aeview_home)

    ids = [f.id for f in report.findings]
    assert sorted(ids) == ["f1", "f2"]  # f1 (absorbing f3), f2 singleton; each exactly once
    survivor = next(f for f in report.findings if f.id == "f1")
    assert survivor.agreement == 2  # f1 + f3, the unknown/repeat ignored


async def test_same_review_grouping_is_not_corroboration(aeview_home, monkeypatch):
    # Reviewer `a` emits two findings the harness groups together; `b` only satisfies the
    # >1-review gate. The survivor has agreement 2 (raw group size) but ONE distinct review,
    # so it must NOT count as corroborated. This fails under the old `f.agreement > 1` formula.
    async def fake_dedup(pool, instance, store, cwd, timeout=600.0):
        return DedupOutcome("ok", [DuplicateGroup(survivor="f1", duplicates=["f2"])], Usage(), "dx")

    monkeypatch.setattr(merge_mod, "run_dedup", fake_dedup)
    reviews = [
        _done("a__h", [_finding("dup1", "high"), _finding("dup2", "high")], "needs-attention", "a"),
        _done("b__h", [_finding("other", "low")], "needs-attention", "b"),
    ]
    report = await _merge(reviews, aeview_home)

    survivor = next(f for f in report.findings if f.id == "f1")
    assert survivor.agreement == 2  # raw group size unchanged
    assert {s.review for s in survivor.sources} == {"a__h"}  # but only one distinct review
    assert "corroborated" not in report.summary


async def test_next_steps_ordered_by_strongest_severity(aeview_home, monkeypatch):
    async def fake_dedup(pool, instance, store, cwd, timeout=600.0):
        return DedupOutcome("ok", [], Usage(), "dx")  # no grouping, keep all findings

    monkeypatch.setattr(merge_mod, "run_dedup", fake_dedup)
    weak = _done("a__h", [_finding("m", "medium")], "needs-attention", "a")
    strong = _done("b__h", [_finding("c", "critical")], "needs-attention", "b")
    nofind = _done("c__h", [], "approve", "c")
    nofind.next_steps = []  # no steps -> omitted entirely

    report = await _merge([weak, strong, nofind], aeview_home)
    assert [b.source for b in report.next_steps] == ["b__h", "a__h"]  # critical before medium
