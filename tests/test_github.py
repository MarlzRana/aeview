from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from aeview import github
from aeview.github import (
    BuiltReview,
    GitHubError,
    PrTarget,
    _anchor_line,
    _diff_anchorable_lines,
    _finding_md,
    build_review,
    delete_comment,
    open_progress,
    post_review,
    render_progress,
    resolve_pr_target,
    roster_status,
)
from aeview.schema import (
    Coverage,
    Dedup,
    Location,
    MergedFinding,
    Report,
    ReviewResult,
    RosterEntry,
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
    line_end=None,
    fid="f1",
    title="t",
    body="body text",
    severity: Severity = "high",
    agreement=1,
    sources=None,
) -> MergedFinding:
    return MergedFinding(
        id=fid,
        title=title,
        body=body,
        severity=severity,
        category="bug",
        confidence=0.9,
        location=Location(
            file=file, line_start=line, line_end=line if line_end is None else line_end
        ),
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


def _payload(built: BuiltReview) -> Any:
    """The review payload as Any — tests index it dynamically; the source keeps the precise
    dict[str, object] annotation (so a bare indexing here isn't a pyright error)."""
    return built.payload


# --- diff line index ------------------------------------------------------------------


def test_diff_anchorable_lines_tracks_new_file_lines_only():
    idx = _diff_anchorable_lines(_DIFF)
    assert idx["pr_file.py"] == {1, 2, 3}  # context line 1 + the two additions
    # old.py's only change is a deletion (old-file side); the surviving context line is its sole
    # new-file line — deletions have no new-file number, so they aren't anchorable.
    assert idx["old.py"] == {1}


def test_diff_anchorable_lines_handles_content_lines_that_look_like_headers():
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
    idx = _diff_anchorable_lines(diff)
    assert set(idx) == {"x.md"}  # the '+++ a bullet...' content line was NOT taken as a header
    assert idx["x.md"] == {1, 2, 3}  # context line + the two real additions


def test_diff_anchorable_lines_unquotes_paths_with_spaces():
    # git C-quotes a path containing a space: `+++ "b/weird name.py"`. Unwrap the quotes so the
    # finding's real path can still match and anchor inline.
    diff = (
        'diff --git "a/weird name.py" "b/weird name.py"\n'
        '--- "a/weird name.py"\n'
        '+++ "b/weird name.py"\n'
        "@@ -1,1 +1,2 @@\n"
        " x = 1\n"
        "+y = 2\n"
    )
    idx = _diff_anchorable_lines(diff)
    assert idx == {"weird name.py": {1, 2}}


def test_diff_anchorable_lines_splits_on_newline_only():
    # str.splitlines() breaks on \v \f \x1c-\x1e \x85 U+2028 U+2029, so a diff *content* line
    # carrying one would be split in two — a phantom iteration that throws new_no off by one for the
    # rest of the hunk. Splitting on "\n" alone (with a \r rstrip) keeps the numbering correct.
    exotic = "a\u2028b\u2029c\fd\x85e\vf\x1c\x1d\x1eg"  # every separator splitlines() splits on
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,1 +1,3 @@\n"
        " keep = 1\n"
        f'+weird = "{exotic}"\n'
        "+after = 2\n"
    )
    idx = _diff_anchorable_lines(diff)
    # context line 1 + the two additions — NOT the phantom lines the split content line would add.
    assert idx == {"x.py": {1, 2, 3}}
    # The line after the exotic one still anchors at 3, not shifted past it.
    assert _anchor_line(Location(file="x.py", line_start=3, line_end=3), idx) == 3


def test_diff_anchorable_lines_handles_crlf_terminated_diff():
    # A CRLF diff leaves a trailing \r on every line after split("\n"); rstrip("\r") drops it so the
    # +++ header path parses and content prefixes still match (matching old splitlines() behaviour).
    diff = (
        "diff --git a/c.py b/c.py\r\n"
        "--- a/c.py\r\n"
        "+++ b/c.py\r\n"
        "@@ -1,1 +1,2 @@\r\n"
        " x = 1\r\n"
        "+y = 2\r\n"
    )
    idx = _diff_anchorable_lines(diff)
    assert idx == {"c.py": {1, 2}}


def test_diff_anchorable_lines_counts_blank_context_line():
    # The `if not line: continue` guard skips ONLY split()'s trailing "" — a blank line in the
    # file is " " (a lone space prefix), which is non-empty and must still count as a new-file
    # line. Pins that boundary so a later `if not line.strip():`/`isspace()` refactor (which looks
    # equivalent but would also swallow blank context lines, mis-anchoring everything after them)
    # fails instead of silently passing.
    diff = (
        "diff --git a/blank.py b/blank.py\n"
        "--- a/blank.py\n"
        "+++ b/blank.py\n"
        "@@ -1,3 +1,4 @@\n"
        " x = 1\n"
        " \n"  # a blank context line: a lone space, NOT an empty string
        "+y = 2\n"
        " z = 3\n"
    )
    idx = _diff_anchorable_lines(diff)
    assert idx == {"blank.py": {1, 2, 3, 4}}  # the blank context line (2) is counted, not skipped
    # The addition after the blank anchors at 3 and the context after it at 4 — not shifted down.
    assert _anchor_line(Location(file="blank.py", line_start=3, line_end=3), idx) == 3
    assert _anchor_line(Location(file="blank.py", line_start=4, line_end=4), idx) == 4


def test_anchor_line_is_right_side_only_and_single_line():
    idx = _diff_anchorable_lines(_DIFF)
    assert _anchor_line(Location(file="pr_file.py", line_start=2, line_end=2), idx) == 2
    # A multi-line finding still anchors at its (single) start line — no cross-hunk range anchor.
    assert _anchor_line(Location(file="pr_file.py", line_start=1, line_end=3), idx) == 1
    # old.py line 2 is a deletion (old-file numbering); findings cite post-change lines, so it
    # never anchors there and falls through to the summary.
    assert _anchor_line(Location(file="old.py", line_start=2, line_end=2), idx) is None
    # A line not in the diff, and an unknown file, are both unanchorable.
    assert _anchor_line(Location(file="pr_file.py", line_start=99, line_end=99), idx) is None
    assert _anchor_line(Location(file="nope.py", line_start=1, line_end=1), idx) is None


# --- build_review ---------------------------------------------------------------------


def test_build_review_anchors_finding_inline():
    built = build_review(_report([_finding(line=2)]), "run1", _DIFF, "sha123")
    assert built.inline_findings == 1 and built.body_findings == 0
    payload = _payload(built)
    assert payload["event"] == "COMMENT" and payload["commit_id"] == "sha123"
    assert len(payload["comments"]) == 1
    c = payload["comments"][0]
    assert c["path"] == "pr_file.py" and c["line"] == 2 and c["side"] == "RIGHT"
    assert "start_line" not in c  # single-line finding -> no range anchor
    assert "🤖 **aeview**" in c["body"]  # the visible badge
    assert "https://github.com/MarlzRana/aeview" not in c["body"]
    assert "<!-- aeview:finding run=run1 id=f1 -->" in c["body"]  # the machine marker
    assert "reviewers: default__claude-code-opus" in c["body"]  # provenance
    assert "🤖 **[aeview](https://github.com/MarlzRana/aeview)**" in payload["body"]
    assert "<!-- aeview:review run=run1 -->" in payload["body"]


def test_build_review_pins_commit_id_to_reviewed_head_or_omits_when_unknown():
    # With a known head: anchor inline and send it as commit_id.
    pinned = _payload(build_review(_report([_finding(line=2)]), "run1", _DIFF, "abc123"))
    assert pinned["commit_id"] == "abc123" and pinned["comments"]
    # Unknown head (couldn't capture / raced): no commit_id AND no inline comments — every finding
    # is summarized in the body so nothing binds to GitHub's latest commit.
    built = build_review(_report([_finding(line=2)]), "run1", _DIFF, None)
    unknown = _payload(built)
    assert "commit_id" not in unknown and "comments" not in unknown
    assert built.inline_findings == 0 and built.body_findings == 1
    # The body explains WHY it's summary-only, and doesn't mislabel it "not anchored to the diff".
    assert "couldn't pin the exact reviewed commit" in unknown["body"]
    assert "not anchored to the diff" not in unknown["body"]


def test_build_review_multiline_finding_anchors_start_and_shows_range_in_body():
    # Single-line anchor at the start (no fragile cross-hunk range); the full span is in the body.
    built = build_review(_report([_finding(line=1, line_end=3)]), "run1", _DIFF, "sha")
    c = _payload(built)["comments"][0]
    assert c["line"] == 1 and c["side"] == "RIGHT"
    assert "start_line" not in c  # no native multi-line range anchor
    assert "lines 1–3" in c["body"]  # the span is conveyed in the comment text instead


def test_build_review_routes_unanchored_finding_to_body():
    built = build_review(_report([_finding(line=99)]), "run1", _DIFF, "sha")
    assert built.inline_findings == 0 and built.body_findings == 1
    payload = _payload(built)
    assert "comments" not in payload  # nothing anchored -> no inline batch
    assert "Findings not anchored to the diff" in payload["body"]
    assert "`pr_file.py:99`" in payload["body"]  # body-listed findings show their location


def test_build_review_groups_same_line_findings_into_one_comment():
    findings = [
        _finding(line=2, fid="f1", title="first"),
        _finding(line=2, fid="f2", title="second"),
    ]
    built = build_review(_report(findings), "run1", _DIFF, "sha")
    assert built.inline_findings == 2
    comments = _payload(built)["comments"]
    assert len(comments) == 1  # both stacked into one thread
    body = comments[0]["body"]
    assert "first" in body and "second" in body
    assert body.count("🤖 **aeview**") == 2  # each finding keeps its own badge, joined by ---


def test_build_review_clean_run_posts_summary_only():
    built = build_review(_report([], verdict="approve"), "run1", _DIFF, "sha")
    assert built.inline_findings == 0 and built.body_findings == 0
    payload = _payload(built)
    assert "comments" not in payload
    assert "🤖 **[aeview](https://github.com/MarlzRana/aeview)**" in payload["body"]
    assert "**approve**" in payload["body"]


def test_finding_md_defuses_mentions_and_injected_markers():
    # Finding text is model output from an untrusted diff, posted under the user's account: it must
    # not ping people (@mentions) or spoof our hidden markers (injected HTML comments).
    f = _finding(
        title="ping @ghost",
        body="hidden <!-- aeview:finding run=evil id=x --> and cc @teamlead",
        fid="f1",
    )
    md = _finding_md(f, "run1", show_location=False)
    zwsp = chr(0x200B)
    assert f"@{zwsp}ghost" in md and f"@{zwsp}teamlead" in md  # mentions broken
    assert "<!-- aeview:finding run=evil" not in md  # injected marker defused (zwsp inserted)
    assert "<!-- aeview:finding run=run1 id=f1 -->" in md  # our real marker (added post-sanitize)


def test_finding_md_clips_oversized_field():
    f = _finding(body="x" * 5000, fid="f1")
    md = _finding_md(f, "run1", show_location=False)
    assert "truncated" in md and "aeview result run1" in md
    assert len(md) < 3000  # the 5000-char body was capped


# --- resolve_pr_target ----------------------------------------------------------------


def test_resolve_pr_target_happy(tmp_path, stub_gh):
    target = resolve_pr_target(tmp_path, None)
    # owner/repo are parsed from the PR url so the API addresses the PR's own repo (fork-safe).
    # The head SHA is NOT resolved here — it's captured with the diff (in scope.py) so posts anchor
    # to the reviewed commit.
    assert target == PrTarget(number=7, url="https://github.com/o/r/pull/7", owner="o", repo="r")


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
    target = PrTarget(number=7, url="https://github.com/o/r/pull/7")
    result = post_review(target, _report([_finding(line=2)]), "run1", _DIFF, "sha9", tmp_path)
    assert result.fallback_reason is None
    assert result.inline == 1 and result.in_body == 0
    assert "pullrequestreview" in result.url
    posted = json.loads(cap.read_text())
    assert posted["event"] == "COMMENT" and posted["commit_id"] == "sha9"  # the reviewed head
    assert posted["comments"][0]["line"] == 2


def test_post_review_falls_back_to_comment_when_review_rejected(tmp_path, stub_gh, monkeypatch):
    monkeypatch.setenv("AEVIEW_GH_API_FAIL", "reviews")  # the reviews POST 422s
    cap = tmp_path / "comment.json"
    monkeypatch.setenv("AEVIEW_GH_CAPTURE_COMMENT", str(cap))
    target = PrTarget(number=7, url="https://github.com/o/r/pull/7")
    findings = [_finding(line=2, fid="f1"), _finding(line=99, fid="f2")]
    result = post_review(target, _report(findings), "run1", _DIFF, "sha9", tmp_path)
    assert result.fallback_reason and result.in_body == 2 and result.inline == 0
    body = json.loads(cap.read_text())["body"]
    assert "🤖 **[aeview](https://github.com/MarlzRana/aeview)**" in body
    assert "could not attach inline comments" in body
    assert "<!-- aeview:finding run=run1 id=f1 -->" in body  # every finding carried over
    assert "<!-- aeview:finding run=run1 id=f2 -->" in body


def test_post_review_ambiguous_fallback_failure_is_ambiguous(tmp_path, stub_gh, monkeypatch):
    # The review was rejected (4xx), but the fallback comment timed out: it may exist, so the
    # error must say ambiguous (a resume must not post again).
    from aeview.github import AmbiguousPostError

    monkeypatch.setenv("AEVIEW_GH_API_FAIL", "reviews")
    monkeypatch.setenv("AEVIEW_GH_COMMENT_FAIL", "post-timeout")
    target = PrTarget(number=7, url="https://github.com/o/r/pull/7")
    with pytest.raises(AmbiguousPostError, match="fallback comment also failed"):
        post_review(target, _report([_finding(line=2)]), "run1", _DIFF, "sha9", tmp_path)


def test_post_review_definite_fallback_failure_is_definite(tmp_path, stub_gh, monkeypatch):
    from aeview.github import AmbiguousPostError

    monkeypatch.setenv("AEVIEW_GH_API_FAIL", "reviews")
    monkeypatch.setenv("AEVIEW_GH_COMMENT_FAIL", "post")  # 403
    target = PrTarget(number=7, url="https://github.com/o/r/pull/7")
    with pytest.raises(GitHubError) as info:
        post_review(target, _report([_finding(line=2)]), "run1", _DIFF, "sha9", tmp_path)
    assert not isinstance(info.value, AmbiguousPostError)


def test_post_review_ambiguous_failure_does_not_fall_back(tmp_path, stub_gh, monkeypatch):
    # A timed-out (or otherwise non-4xx) reviews POST is ambiguous — GitHub may have created it
    # server-side. Falling back to a comment would double-post, so we raise and post nothing more.
    # (A definite 4xx rejection, by contrast, DOES fall back — see the test above.)
    monkeypatch.setenv("AEVIEW_GH_API_FAIL", "timeout")  # exits 124, no "HTTP 4xx" in stderr
    cap = tmp_path / "comment.json"
    monkeypatch.setenv("AEVIEW_GH_CAPTURE_COMMENT", str(cap))
    target = PrTarget(number=7, url="https://github.com/o/r/pull/7")
    with pytest.raises(GitHubError, match="ambiguously"):
        post_review(target, _report([_finding(line=2)]), "run1", _DIFF, "sha9", tmp_path)
    assert not cap.exists()  # no fallback comment -> no duplicate artifact


# --- progress comment -----------------------------------------------------------------

_TARGET = PrTarget(number=7, url="https://github.com/o/r/pull/7", owner="o", repo="r")
_ROSTER = [
    RosterEntry(id="a__claude-code-opus", reviewer="a", harness="claude-code", model="opus"),
    RosterEntry(id="b__codex-gpt", reviewer="b", harness="codex", model="gpt", thinking="high"),
]


def _review(entry: RosterEntry, status, findings=()) -> ReviewResult:
    return ReviewResult(
        id=entry.id,
        reviewer=entry.reviewer,
        harness=entry.harness,
        model=entry.model,
        status=status,
        findings=list(findings),
    )


def _log(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_render_progress_shows_bar_roster_status_and_marker():
    reviews = {
        _ROSTER[0].id: _review(_ROSTER[0], "done"),
        _ROSTER[1].id: _review(_ROSTER[1], "running"),
    }
    body = render_progress("run1", _ROSTER, reviews, "reviewing", "deadbeefcafe0123")
    assert "⏳ **reviewing…**" in body
    assert "`" + "█" * 10 + "░" * 10 + "` 1/2 reviews finished" in body
    assert "| a | claude-code | opus | ✅ done · 0 findings |" in body
    assert "| b | codex | gpt (high) | 🔄 running |" in body
    assert "commit `deadbeefcafe`" in body  # short sha
    assert body.endswith("<!-- aeview:progress run=run1 -->")


def test_render_progress_pending_and_interrupted_hint():
    reviews = {_ROSTER[0].id: _review(_ROSTER[0], "running")}
    body = render_progress("run1", _ROSTER, reviews, "interrupted", None)
    assert "⚠️ **interrupted**" in body and "0/2 reviews finished" in body
    assert "| a | claude-code | opus | ⚠️ interrupted |" in body  # cut off, not still running
    assert "⏸️ pending" in body
    assert "Resume with `aeview resume run1`." in body
    assert "replaced by the review" not in body  # terminal phases don't promise a replacement


def test_render_progress_escapes_table_cells():
    entry = RosterEntry(id="x__pi-m", reviewer="x", harness="pi", model="a|b @team")
    body = render_progress("run1", [entry], {}, "reviewing", None)
    assert "a\\|b @\u200bteam" in body  # pipe can't break the row; mention defused


def test_open_progress_posts_a_new_comment(tmp_path, stub_gh, monkeypatch):
    log = tmp_path / "gh.jsonl"
    monkeypatch.setenv("AEVIEW_GH_LOG", str(log))
    progress = open_progress(_TARGET, "run1", _ROSTER, "sha", tmp_path)
    assert progress.comment_id == 9001
    [call] = _log(log)
    assert call["method"] == "POST" and call["endpoint"] == "repos/o/r/issues/7/comments"
    assert "<!-- aeview:progress run=run1 -->" in json.loads(call["body"])["body"]


def test_open_progress_reuses_existing_comment(tmp_path, stub_gh, monkeypatch):
    log = tmp_path / "gh.jsonl"
    monkeypatch.setenv("AEVIEW_GH_LOG", str(log))
    progress = open_progress(_TARGET, "run1", _ROSTER, "sha", tmp_path, existing_id=42)
    assert progress.comment_id == 42
    assert [(c["method"], c["endpoint"]) for c in _log(log)] == [
        ("PATCH", "repos/o/r/issues/comments/42")
    ]


def test_open_progress_replaces_a_vanished_comment(tmp_path, stub_gh, monkeypatch):
    log = tmp_path / "gh.jsonl"
    monkeypatch.setenv("AEVIEW_GH_LOG", str(log))
    monkeypatch.setenv("AEVIEW_GH_COMMENT_FAIL", "edit")  # the old comment 404s (e.g. swept)
    progress = open_progress(_TARGET, "run1", _ROSTER, "sha", tmp_path, existing_id=42)
    assert progress.comment_id == 9001
    assert [c["method"] for c in _log(log)] == ["PATCH", "POST"]


def test_open_progress_keeps_the_old_comment_on_a_non_404_edit_failure(
    tmp_path, stub_gh, monkeypatch
):
    # A 5xx/timeout doesn't prove the comment is gone; a second POST would orphan the first.
    log = tmp_path / "gh.jsonl"
    monkeypatch.setenv("AEVIEW_GH_LOG", str(log))
    monkeypatch.setenv("AEVIEW_GH_COMMENT_FAIL", "edit-5xx")
    progress = open_progress(_TARGET, "run1", _ROSTER, "sha", tmp_path, existing_id=42)
    assert progress.comment_id == 42
    assert progress.error is not None and "HTTP 502" in progress.error
    assert [c["method"] for c in _log(log)] == ["PATCH"]


def test_open_progress_raises_when_post_fails(tmp_path, stub_gh, monkeypatch):
    monkeypatch.setenv("AEVIEW_GH_COMMENT_FAIL", "post")
    with pytest.raises(GitHubError, match="HTTP 403"):
        open_progress(_TARGET, "run1", _ROSTER, "sha", tmp_path)


def test_delete_comment_treats_404_as_gone(tmp_path, monkeypatch):
    calls = iter(
        [
            github.ProcResult(0, "", ""),
            github.ProcResult(1, "", "gh: Not Found (HTTP 404)"),
            github.ProcResult(1, "", "gh: Server Error (HTTP 502)"),
        ]
    )
    monkeypatch.setattr(github, "run_sync", lambda *a, **k: next(calls))
    assert delete_comment(_TARGET, 1, tmp_path)
    assert delete_comment(_TARGET, 1, tmp_path)
    assert not delete_comment(_TARGET, 1, tmp_path)


def test_delete_comment_hides_as_outdated_when_delete_is_refused(tmp_path, monkeypatch):
    calls: list[tuple[list[str], str | None]] = []
    replies = iter(
        [
            github.ProcResult(1, "", "gh: Must have admin rights (HTTP 403)"),
            github.ProcResult(0, json.dumps({"id": 1, "node_id": "IC_x"}), ""),
            github.ProcResult(
                0,
                json.dumps(
                    {"data": {"minimizeComment": {"minimizedComment": {"isMinimized": True}}}}
                ),
                "",
            ),
        ]
    )

    def fake_run(args, cwd=None, timeout=None, input_text=None):
        calls.append((args, input_text))
        return next(replies)

    monkeypatch.setattr(github, "run_sync", fake_run)
    assert delete_comment(_TARGET, 1, tmp_path)
    graphql_args, graphql_body = calls[-1]
    assert "graphql" in graphql_args
    sent = json.loads(graphql_body or "{}")
    assert "classifier: OUTDATED" in sent["query"] and sent["variables"] == {"id": "IC_x"}


def test_delete_comment_refused_and_unhideable_is_not_gone(tmp_path, monkeypatch):
    replies = iter(
        [
            github.ProcResult(1, "", "gh: Must have admin rights (HTTP 403)"),
            github.ProcResult(0, json.dumps({"id": 1, "node_id": "IC_x"}), ""),
            github.ProcResult(1, json.dumps({"data": {"minimizeComment": None}}), "gh: forbidden"),
        ]
    )
    monkeypatch.setattr(github, "run_sync", lambda *a, **k: next(replies))
    assert not delete_comment(_TARGET, 1, tmp_path)  # left for the next sweep to retry


@pytest.mark.parametrize(
    "get_reply",
    [
        github.ProcResult(1, "", "gh: Not Found (HTTP 404)"),  # deleted concurrently
        github.ProcResult(0, json.dumps({"id": 1}), ""),  # no node_id
        github.ProcResult(0, "not json", ""),
        github.ProcResult(0, "[]", ""),  # JSON, but not an object
    ],
)
def test_delete_comment_without_a_node_id_never_attempts_the_hide(tmp_path, monkeypatch, get_reply):
    calls: list[list[str]] = []
    replies = iter([github.ProcResult(1, "", "gh: Must have admin rights (HTTP 403)"), get_reply])

    def fake_run(args, cwd=None, timeout=None, input_text=None):
        calls.append(args)
        return next(replies)

    monkeypatch.setattr(github, "run_sync", fake_run)
    assert not delete_comment(_TARGET, 1, tmp_path)
    assert not any("graphql" in args for args in calls)


def test_delete_comment_tolerates_a_null_minimized_comment(tmp_path, monkeypatch):
    # The schema allows a null leaf with gh exiting 0; that's "not hidden", never an exception.
    replies = iter(
        [
            github.ProcResult(1, "", "gh: Must have admin rights (HTTP 403)"),
            github.ProcResult(0, json.dumps({"id": 1, "node_id": "IC_x"}), ""),
            github.ProcResult(
                0, json.dumps({"data": {"minimizeComment": {"minimizedComment": None}}}), ""
            ),
        ]
    )
    monkeypatch.setattr(github, "run_sync", lambda *a, **k: next(replies))
    assert not delete_comment(_TARGET, 1, tmp_path)


async def test_progress_coalesces_bursts_and_ends_on_latest_state(tmp_path, monkeypatch):
    import time

    edits: list[str] = []

    def slow_edit(target, comment_id, body, cwd):
        time.sleep(0.05)  # an in-flight PATCH while more reviews finish
        edits.append(body)
        return github.ProcResult(0, "", "")

    monkeypatch.setattr(github, "_edit_comment", slow_edit)
    progress = github.ProgressComment(_TARGET, "run1", _ROSTER, None, tmp_path, comment_id=1)
    progress.record(_review(_ROSTER[0], "running"))
    await asyncio.sleep(0.01)  # let that first edit go in flight
    progress.record(_review(_ROSTER[1], "running"))
    for entry in _ROSTER:
        progress.record(_review(entry, "done"))
    progress.set_phase("merging")
    await progress.drain()
    assert len(edits) == 2  # the first edit, then one coalesced edit for everything after it
    assert "merging findings" in edits[-1] and "2/2 reviews finished" in edits[-1]
    assert progress.error is None


async def test_progress_records_first_edit_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        github, "_edit_comment", lambda *a: github.ProcResult(1, "", "gh: boom (HTTP 500)")
    )
    progress = github.ProgressComment(_TARGET, "run1", _ROSTER, None, tmp_path, comment_id=1)
    progress.record(_review(_ROSTER[0], "running"))
    await progress.drain()
    assert progress.error == "gh: boom (HTTP 500)"


# --- reviewers table in the final review -------------------------------------------------


def test_review_summary_shows_the_reviewers_table_with_final_statuses():
    # The review replaces the progress comment, so it carries the same roster table — with final
    # statuses: a review still `running` at post time was cut off, not in flight.
    reviews = {
        _ROSTER[0].id: _review(_ROSTER[0], "done"),
        _ROSTER[1].id: _review(_ROSTER[1], "failed"),
    }
    built = build_review(
        _report([_finding(line=2)], failed=1),
        "run1",
        _DIFF,
        "sha",
        roster=roster_status(_ROSTER, reviews),
    )
    body = _payload(built)["body"]
    assert "#### Reviewers" in body
    assert "| Reviewer | Harness | Model | Status |" in body
    assert "| a | claude-code | opus | ✅ done · 0 findings |" in body
    assert "| b | codex | gpt (high) | ❌ failed |" in body
    assert body.index("#### Reviewers") < body.index("<!-- aeview:review")


def test_review_summary_marks_a_still_running_review_interrupted():
    reviews = {_ROSTER[0].id: _review(_ROSTER[0], "running")}
    body = _payload(
        build_review(_report([]), "run1", _DIFF, "sha", roster=roster_status(_ROSTER, reviews))
    )["body"]
    assert "| a | claude-code | opus | ⚠️ interrupted |" in body
    assert "| b | codex | gpt (high) | ⏸️ pending |" in body  # never started


def test_review_summary_without_a_roster_has_no_table():
    assert "#### Reviewers" not in _payload(build_review(_report([]), "run1", _DIFF, "sha"))["body"]


def test_fallback_comment_also_shows_the_reviewers_table(tmp_path, stub_gh, monkeypatch):
    monkeypatch.setenv("AEVIEW_GH_API_FAIL", "reviews")  # the reviews POST 422s
    cap = tmp_path / "comment.json"
    monkeypatch.setenv("AEVIEW_GH_CAPTURE_COMMENT", str(cap))
    reviews = {e.id: _review(e, "done") for e in _ROSTER}
    target = PrTarget(number=7, url="https://github.com/o/r/pull/7")
    post_review(
        target,
        _report([_finding(line=2)]),
        "run1",
        _DIFF,
        "sha9",
        tmp_path,
        roster=roster_status(_ROSTER, reviews),
    )
    body = json.loads(cap.read_text())["body"]
    assert "could not attach inline comments" in body
    assert "| b | codex | gpt (high) | ✅ done · 0 findings |" in body
