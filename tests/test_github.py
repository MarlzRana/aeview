from __future__ import annotations

import json

import pytest

from aeview.github import (
    GitHubError,
    PrTarget,
    _anchor,
    _diff_added_lines,
    build_review,
    post_review,
    resolve_pr_target,
)
from aeview.schema import (
    Coverage,
    Dedup,
    Location,
    MergedFinding,
    Report,
    Severity,
    Source,
    UsageBreakdown,
    Verdict,
)

# A two-file diff: an addition (RIGHT lines) and a deletion (LEFT line).
_DIFF = (
    "diff --git a/pr_file.py b/pr_file.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/pr_file.py\n"
    "+++ b/pr_file.py\n"
    "@@ -1,2 +1,3 @@\n"
    " x = 1\n"
    "+y = 2\n"
    "+z = 3\n"
    "diff --git a/old.py b/old.py\n"
    "--- a/old.py\n"
    "+++ b/old.py\n"
    "@@ -1,2 +1,1 @@\n"
    " keep = 1\n"
    "-gone = 2\n"
)


def _finding(
    *,
    file="pr_file.py",
    line=2,
    fid="f1",
    title="t",
    severity: Severity = "high",
    agreement=1,
    sources=None,
) -> MergedFinding:
    return MergedFinding(
        id=fid,
        title=title,
        body="body text",
        severity=severity,
        category="bug",
        confidence=0.9,
        location=Location(file=file, line_start=line, line_end=line),
        recommendation="do the fix",
        agreement=agreement,
        sources=sources
        or [Source(review="default__claude-code-opus", severity=severity, confidence=0.9)],
    )


def _report(findings, *, verdict: Verdict = "needs-attention", contributed=1, failed=0) -> Report:
    return Report(
        verdict=verdict,
        summary="a summary line",
        findings=findings,
        next_steps=[],
        coverage=Coverage(contributed=contributed, failed=failed),
        dedup=Dedup(status="ok"),
        usage=UsageBreakdown(),
    )


# --- diff line index ------------------------------------------------------------------


def test_diff_added_lines_tracks_new_file_lines_only():
    idx = _diff_added_lines(_DIFF)
    assert idx["pr_file.py"] == {1, 2, 3}  # context line 1 + the two additions
    # old.py's only change is a deletion (old-file side); the surviving context line is its sole
    # new-file line — deletions have no new-file number, so they aren't anchorable.
    assert idx["old.py"] == {1}


def test_diff_added_lines_handles_content_lines_that_look_like_headers():
    # An added line whose own text starts with '++ ' makes the diff line read '+++ ...'; inside a
    # hunk that's content, not a new-file header. The parser must keep counting, not reset on it.
    diff = (
        "diff --git a/x.md b/x.md\n"
        "--- a/x.md\n"
        "+++ b/x.md\n"
        "@@ -1,1 +1,3 @@\n"
        " title\n"
        "+++ a bullet whose text starts with plus-plus\n"
        "+normal added line\n"
    )
    idx = _diff_added_lines(diff)
    assert set(idx) == {"x.md"}  # the '+++ a bullet...' content line was NOT taken as a header
    assert idx["x.md"] == {1, 2, 3}  # context line + the two real additions


def test_anchor_is_right_side_only():
    idx = _diff_added_lines(_DIFF)
    assert _anchor(Location(file="pr_file.py", line_start=2, line_end=2), idx) == 2
    # old.py line 2 is a deletion (old-file numbering) — findings cite post-change lines, so we
    # never anchor there; it falls through to the summary instead.
    assert _anchor(Location(file="old.py", line_start=2, line_end=2), idx) is None
    # A line not in the diff, and an unknown file, are both unanchorable.
    assert _anchor(Location(file="pr_file.py", line_start=99, line_end=99), idx) is None
    assert _anchor(Location(file="nope.py", line_start=1, line_end=1), idx) is None


# --- build_review ---------------------------------------------------------------------


def test_build_review_anchors_finding_inline():
    built = build_review(_report([_finding(line=2)]), "run1", "sha123", _DIFF)
    assert built.inline_findings == 1 and built.body_findings == 0
    payload = built.payload
    assert payload["event"] == "COMMENT" and payload["commit_id"] == "sha123"
    assert len(payload["comments"]) == 1
    c = payload["comments"][0]
    assert c["path"] == "pr_file.py" and c["line"] == 2 and c["side"] == "RIGHT"
    assert "🤖 **aeview**" in c["body"]  # the visible badge
    assert "<!-- aeview:finding run=run1 id=f1 -->" in c["body"]  # the machine marker
    assert "reviewers: default__claude-code-opus" in c["body"]  # provenance
    assert "<!-- aeview:review run=run1 -->" in payload["body"]


def test_build_review_routes_unanchored_finding_to_body():
    built = build_review(_report([_finding(line=99)]), "run1", "sha", _DIFF)
    assert built.inline_findings == 0 and built.body_findings == 1
    assert "comments" not in built.payload  # nothing anchored -> no inline batch
    body = built.payload["body"]
    assert "Findings not anchored to the diff" in body
    assert "`pr_file.py:99`" in body  # body-listed findings show their location


def test_build_review_groups_same_line_findings_into_one_comment():
    findings = [
        _finding(line=2, fid="f1", title="first"),
        _finding(line=2, fid="f2", title="second"),
    ]
    built = build_review(_report(findings), "run1", "sha", _DIFF)
    assert built.inline_findings == 2
    assert len(built.payload["comments"]) == 1  # both stacked into one thread
    body = built.payload["comments"][0]["body"]
    assert "first" in body and "second" in body
    assert body.count("🤖 **aeview**") == 2  # each finding keeps its own badge, joined by ---


def test_build_review_clean_run_posts_summary_only():
    built = build_review(_report([], verdict="approve"), "run1", "sha", _DIFF)
    assert built.inline_findings == 0 and built.body_findings == 0
    assert "comments" not in built.payload
    assert "**approve**" in built.payload["body"]


# --- resolve_pr_target ----------------------------------------------------------------


def test_resolve_pr_target_happy(tmp_path, stub_gh):
    target = resolve_pr_target(tmp_path, None)
    assert target == PrTarget(
        number=7, head_sha="deadbeefcafe", url="https://github.com/o/r/pull/7"
    )


def test_resolve_pr_target_no_pr_errors(tmp_path, stub_gh, monkeypatch):
    monkeypatch.setenv("AEVIEW_GH_NO_PR", "1")
    with pytest.raises(GitHubError, match="needs an open PR|found none"):
        resolve_pr_target(tmp_path, None)


def test_resolve_pr_target_closed_pr_errors(tmp_path, stub_gh, monkeypatch):
    monkeypatch.setenv("AEVIEW_GH_PR_STATE", "MERGED")
    with pytest.raises(GitHubError, match="open PR.*merged"):
        resolve_pr_target(tmp_path, None)


# --- post_review ----------------------------------------------------------------------


def test_post_review_posts_one_review(tmp_path, stub_gh, monkeypatch):
    cap = tmp_path / "review.json"
    monkeypatch.setenv("AEVIEW_GH_CAPTURE", str(cap))
    target = PrTarget(number=7, head_sha="sha9", url="https://github.com/o/r/pull/7")
    result = post_review(target, _report([_finding(line=2)]), "run1", _DIFF, tmp_path)
    assert not result.fell_back
    assert result.inline == 1 and result.in_body == 0
    assert "pullrequestreview" in result.url
    posted = json.loads(cap.read_text())
    assert posted["event"] == "COMMENT" and posted["commit_id"] == "sha9"
    assert posted["comments"][0]["line"] == 2


def test_post_review_falls_back_to_comment_when_review_rejected(tmp_path, stub_gh, monkeypatch):
    monkeypatch.setenv("AEVIEW_GH_API_FAIL", "reviews")  # the reviews POST 422s
    cap = tmp_path / "comment.json"
    monkeypatch.setenv("AEVIEW_GH_CAPTURE_COMMENT", str(cap))
    target = PrTarget(number=7, head_sha="sha9", url="https://github.com/o/r/pull/7")
    findings = [_finding(line=2, fid="f1"), _finding(line=99, fid="f2")]
    result = post_review(target, _report(findings), "run1", _DIFF, tmp_path)
    assert result.fell_back and result.in_body == 2 and result.inline == 0
    body = json.loads(cap.read_text())["body"]
    assert "could not attach inline comments" in body
    assert "<!-- aeview:finding run=run1 id=f1 -->" in body  # every finding carried over
    assert "<!-- aeview:finding run=run1 id=f2 -->" in body
