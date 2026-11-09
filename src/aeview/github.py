"""Post a merged Report onto a GitHub pull request as a single review.

Only `aeview run --scope pr --post-comments` reaches here. We post ONE GitHub PR review per run
(`event=COMMENT` — it never approves or requests changes, so it can never gate a merge), carrying:
  - a summary body (verdict + summary + coverage + any findings that couldn't be anchored), and
  - one inline comment per finding, anchored to its file/line in the PR diff.

The review is authored by whoever `gh` is authenticated as (the user, not an "aeview" identity), so
every comment is self-labelled: a visible `aeview (automated review panel)` badge plus per-finding
reviewer provenance for humans, and a hidden `<!-- aeview:... -->` marker for tooling.

GitHub anchors inline comments only to lines that appear in the PR diff, and the create-review call
is all-or-nothing (one out-of-diff line 422s the whole batch). So we pre-validate every anchor
against the diff and route the rest into the body; if the post still fails, we fall back to a single
top-level comment carrying every finding (the findings are never silently dropped).

Live progress: before the fan-out, a separate issue comment shows the roster and a progress bar, is
edited as reviews finish, and is deleted once the review is posted (see the progress section).

Threading: each run is its own review, so re-running leaves a fresh thread per finding even on a
line a previous run already commented on — the series of reviews is the PR's audit trail. Within one
review, findings sharing an anchor are merged into one comment (the API can't thread siblings that
don't have ids yet).

All `gh` interaction for posting is funnelled through here, keeping merge.py/report.py I/O-free.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .process import ProcResult, run_sync
from .report import report_verdict_label
from .schema import Location, MergedFinding, PrTarget, Report, ReviewResult, RosterEntry

# Each roster entry with its review result (None = not started), for the reviewers table.
type RosterStatus = Sequence[tuple[RosterEntry, ReviewResult | None]]

# Visible badge so a human reading the PR can tell aeview spoke (the comment is authored by the
# gh-authenticated user, not a distinct bot account); the hidden marker is the machine signal.
_BADGE = "🤖 **aeview** (automated review panel)"
_SUMMARY_BADGE = "🤖 **[aeview](https://github.com/MarlzRana/aeview)** (automated review panel)"
_REVIEW_MARKER = "<!-- aeview:review run={run_id} -->"
_FINDING_MARKER = "<!-- aeview:finding run={run_id} id={finding_id} -->"

_GH_API_HEADERS = (
    "-H",
    "Accept: application/vnd.github+json",
    "-H",
    "X-GitHub-Api-Version: 2022-11-28",
)
# Posting is a quick HTTP call made after the run already finished; bound it so a stalled gh
# (auth prompt, network) can't hang the CLI — critical for the loop-until-clean automation.
_GH_TIMEOUT_S = 60
# Cap model-controlled text in a posted comment; the untruncated finding stays in report.json, and
# the cap keeps a verbose review well under GitHub's per-comment size limit.
_FIELD_CAP = 1500
# `@` before a word char is a GitHub mention — a zero-width space defuses it (finding text is model
# output from an untrusted diff, posted under the user's account; it must not ping people).
_MENTION = re.compile(r"@(?=[A-Za-z0-9])")
# The PR's own repo, parsed from its html_url (fork-safe: `gh api {owner}/{repo}` would resolve the
# local fork, not the upstream the PR lives on). Falls back to gh's placeholders if unparsed.
_PR_URL = re.compile(r"://[^/]+/([^/]+)/([^/]+)/pull/\d+")
# A 4xx from `gh api` means the request was rejected and the review was NOT created (safe to salvage
# via a comment). `gh` prints the status like "(HTTP 422)" on stderr; anything else is ambiguous.
_CLIENT_REJECTION = re.compile(r"HTTP 4\d\d")


class GitHubError(Exception):
    """Raised when the PR target can't be resolved or posting fails irrecoverably."""


class AmbiguousPostError(GitHubError):
    """The review POST failed in a way that doesn't prove GitHub rejected it (timeout, 5xx,
    network): the review may exist, so it must not be retried automatically."""


@dataclass(slots=True)
class PostResult:
    """What post_review did, for the CLI's stderr status line."""

    url: str
    inline: int  # findings posted as inline comments
    in_body: int  # findings listed in the summary body (unanchored, or all of them on fallback)
    # Set (and the review became a single top-level comment) iff the inline post was rejected and we
    # degraded; None on the normal path. One field, since it fully encodes "did we fall back".
    fallback_reason: str | None = None


# --- PR target resolution -------------------------------------------------------------


def resolve_pr_target(cwd: Path, value: str | None) -> PrTarget:
    """Resolve the open PR to post to. `value` is the --scope pr value (a PR number, or None for the
    current branch's PR). Raises GitHubError if gh fails, no PR exists, or the PR isn't open — the
    run must not spend a whole fan-out only to discover there's nowhere to post."""
    args = ["gh", "pr", "view"]
    if value:
        args.append(value)
    args += ["--json", "number,state,url"]
    res = run_sync(args, cwd=cwd, timeout=_GH_TIMEOUT_S)
    if res.returncode != 0:
        where = f"PR #{value}" if value else "an open PR for the current branch"
        detail = res.stderr.strip()
        raise GitHubError(
            f"--post-comments needs {where}, but `gh pr view` found none"
            + (f" ({detail})" if detail else "")
            + ". Open a PR first (e.g. `gh pr create`), or re-run without --post-comments."
        )
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubError(f"could not parse `gh pr view` output: {exc}") from exc
    if data.get("state") != "OPEN":
        state = str(data.get("state", "unknown")).lower()
        raise GitHubError(
            f"--post-comments only posts to an open PR, but PR #{data.get('number')} is {state}."
        )
    number, url = data.get("number"), data.get("url")
    if not (isinstance(number, int) and isinstance(url, str)):
        raise GitHubError("`gh pr view` returned an incomplete PR record (number/url).")
    # Address the PR's own repo (parsed from its url), not gh's cwd-derived {owner}/{repo}.
    owner, repo = "{owner}", "{repo}"
    if m := _PR_URL.search(url):
        owner, repo = m.group(1), m.group(2)
    return PrTarget(number=number, url=url, owner=owner, repo=repo)


# --- diff line index (which lines can carry an inline comment) ------------------------

_HUNK = re.compile(r"^@@ -(?:\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _diff_anchorable_lines(diff: str) -> dict[str, list[range]]:
    """Map each changed file to its hunks, each as the range of new-file line numbers a comment can
    anchor to (additions + context). Within a hunk those lines are contiguous — deletions have no
    new-file number and don't advance it — so a range per hunk is exact. Findings cite post-change
    lines, so anchoring is RIGHT-side only: an old-file line (a deletion) is never anchorable.

    Header lines (`+++ `) and hunk-body content are told apart by hunk state, not prefix alone: an
    added line whose own text starts with `++ ` reads `+++ ...` in the diff — inside a hunk that's
    content, and treating it as a header would reset the counter and corrupt every later line."""
    index: dict[str, list[range]] = {}
    current: list[range] | None = None
    in_hunk = False
    hunk_start = new_no = 0

    def close_hunk(dest: list[range] | None, start: int, stop: int) -> None:
        if dest is not None and stop > start:  # skip deletion-only hunks (no new-file lines)
            dest.append(range(start, stop))

    # Split on "\n" only, NOT str.splitlines(): the latter also breaks on \v \f \x1c-\x1e
    # \x85 U+2028 U+2029, so one of those inside a content line would split it in two, adding a
    # phantom iteration that bumps new_no and mis-anchors every later line in the hunk.
    for line in diff.split("\n"):
        line = line.rstrip("\r")  # keep CRLF diffs parsing as splitlines() did (drop the diff CR)
        if not line:
            continue  # split()'s trailing ""; a real diff line is " " or "+", never empty
        if line.startswith("diff --git"):
            close_hunk(current if in_hunk else None, hunk_start, new_no)
            current, in_hunk = None, False  # next file; its +++ header + counts come before @@
            continue
        if line.startswith("@@"):
            close_hunk(current if in_hunk else None, hunk_start, new_no)
            # An unparseable header (e.g. a combined diff's `@@@`) ends the hunk rather than
            # leaving the previous one open to be recorded twice.
            m = _HUNK.match(line)
            in_hunk = m is not None
            if m:
                hunk_start = new_no = int(m.group(1))  # the +new hunk start (RIGHT numbering)
            continue
        if not in_hunk:  # pre-hunk preamble: the index/mode/+++/--- header lines
            if line.startswith("+++ "):
                raw = line[4:].split("\t", 1)[0]
                if raw.startswith('"') and raw.endswith('"'):
                    raw = raw[1:-1]  # git C-quotes paths with spaces/specials; unwrap the quotes
                path = raw.removeprefix("b/")
                current = None if path == "/dev/null" else index.setdefault(path, [])
            continue
        if current is None or line.startswith(("-", "\\")):
            continue  # a deletion (old-file only) or the "\ No newline" marker: no new-file line
        new_no += 1  # an added or context line: present in the new file
    close_hunk(current if in_hunk else None, hunk_start, new_no)
    return index


def _anchor_span(loc: Location, index: dict[str, list[range]]) -> tuple[int, int] | None:
    """The (first, last) new-file lines an inline comment covers (always RIGHT side): the part of
    the finding's range inside the first hunk it overlaps, or None if it touches no hunk — then it
    falls to the summary. GitHub rejects a line outside every hunk, and a range whose ends sit in
    different hunks 422s the *entire* review batch (dumping every inline comment to the fallback),
    so the span never leaves that one hunk; the full range stays in the comment (`_provenance`)."""
    last = max(loc.line_start, loc.line_end)  # a reversed range degrades to its start line
    for hunk in index.get(loc.file, ()):  # hunks are in file order: the first overlap wins
        first, end = max(loc.line_start, hunk.start), min(last, hunk.stop - 1)
        if first <= end:
            return first, end
    return None


# --- review payload -------------------------------------------------------------------


@dataclass(slots=True)
class BuiltReview:
    payload: dict[str, object]
    inline_findings: int
    body_findings: int


def _sanitize(text: str) -> str:
    """Defuse model-controlled text before posting it to GitHub under the user's account: break
    @mentions (they would ping people) and HTML-comment delimiters (they could spoof our hidden
    markers or hide content). A zero-width space is invisible but breaks both constructs. GitHub
    already neutralizes other raw HTML."""
    zwsp = chr(0x200B)  # zero-width space: invisible, but breaks the mention / comment token
    text = _MENTION.sub(f"@{zwsp}", text)
    return text.replace("<!--", f"<!{zwsp}--").replace("-->", f"--{zwsp}>")


def _clip(text: str, run_id: str) -> str:
    """Bound one model-controlled field so a verbose finding can't blow the comment size limit; the
    untruncated text stays in the persisted report."""
    if len(text) <= _FIELD_CAP:
        return text
    return text[:_FIELD_CAP] + f"… _(truncated — full text in `aeview result {run_id}`)_"


def _location_md(loc: Location) -> str:
    # loc.file is model output; strip backticks so it can't break out of its code span.
    loc_str = f"{loc.file.replace('`', '')}:{loc.line_start}"
    if loc.line_end != loc.line_start:
        loc_str += f"-{loc.line_end}"
    return f"`{loc_str}`"


def _provenance(f: MergedFinding) -> str:
    """One line naming the reviewer(s) that raised this finding (from its merge sources), so it's
    clear which panel member spoke even though the comment is authored by the gh user."""
    reviewers = ", ".join(s.review for s in f.sources) or "—"
    bits = [f"reviewers: {reviewers}", f"confidence {f.confidence:.2f}"]
    loc = f.location
    # The anchor may be clipped to one hunk (or absent, in the summary): always show the full span.
    if loc.line_end != loc.line_start:
        bits.append(f"lines {loc.line_start}–{loc.line_end}")
    if f.agreement > 1:
        bits.append(f"agreement ×{f.agreement}")
    return "<sub>" + " · ".join(bits) + "</sub>"


def _slot_text(f: MergedFinding, key: str) -> str | None:
    """A descriptive slot (title/body/category/recommendation) as display text, or None if the
    reviewer dropped it via custom-schemas. A reshaped (non-string) slot — e.g. a rubric object —
    is rendered as compact JSON so it still posts cleanly rather than crashing on `.strip()`."""
    value = f.slot(key)
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _finding_md(f: MergedFinding, run_id: str, *, show_location: bool) -> str:
    """Render one finding. `show_location` is for body-listed (unanchored) findings, where there is
    no inline anchor to convey the file/line; inline comments omit it (the anchor shows it).

    title/body/recommendation/category are model output derived from an untrusted diff, so they're
    sanitized (mentions/markers) and length-capped before they're posted under the user's account. A
    custom-schemas reviewer may reshape or drop any of them, so each is read defensively."""
    title = _slot_text(f, "title") or "(untitled)"
    category = _slot_text(f, "category")
    body = _slot_text(f, "body")
    recommendation = _slot_text(f, "recommendation")
    # category/title are now model-controlled too (custom-schemas can reshape/replace them) and
    # unbounded, so sanitize AND length-cap them like body/recommendation — a fixed enum + a
    # schema-capped title used to make this safe; a custom value is untrusted and unbounded.
    head = (
        f"`{f.severity}`"
        + (f" · {_clip(_sanitize(category), run_id)}" if category else "")
        + f" — **{_clip(_sanitize(title.strip()), run_id)}**"
    )
    if show_location:
        head = f"{_location_md(f.location)} · {head}"
    blocks = [
        _BADGE,
        head,
        _clip(_sanitize(body.strip()), run_id) if body else None,
        f"**Fix:** {_clip(_sanitize(recommendation.strip()), run_id)}" if recommendation else None,
        _provenance(f),
        _FINDING_MARKER.format(run_id=run_id, finding_id=f.id),
    ]
    return "\n\n".join(b for b in blocks if b)


def _review_body(
    report: Report,
    run_id: str,
    listed: list[MergedFinding],
    roster: RosterStatus,
    *,
    note: str | None = None,
) -> str:
    """The review's top-level summary. `listed` findings are rendered in full here — the unanchored
    ones for a normal review, or every finding on the fallback path (where `note` explains why).
    The roster table mirrors the progress comment this review replaces, with final statuses."""
    label = report_verdict_label(report)
    parts = [
        f"{_SUMMARY_BADGE} — **{label}**",
        report.summary.strip() or "(no summary)",
    ]
    cov = report.coverage
    if cov.failed:
        parts.append(f"Coverage: {cov.contributed} review(s) contributed, {cov.failed} failed.")
    if roster:
        parts.append("#### Reviewers\n\n" + _roster_table(roster, live=False))
    if note:
        parts.append(note)
    if listed:
        parts.append("#### Findings" if note else "#### Findings not anchored to the diff")
        parts.extend(_finding_md(f, run_id, show_location=True) for f in listed)
    parts.append(_REVIEW_MARKER.format(run_id=run_id))
    return "\n\n".join(parts)


def _inline_comment(
    path: str, first: int, last: int, findings: list[MergedFinding], run_id: str
) -> dict[str, object]:
    # Findings cite post-change lines, so both ends anchor on the new file (RIGHT side).
    comment: dict[str, object] = {"path": path, "line": last, "side": "RIGHT"}
    if first < last:
        comment |= {"start_line": first, "start_side": "RIGHT"}
    comment["body"] = "\n\n---\n\n".join(
        _finding_md(f, run_id, show_location=False) for f in findings
    )
    return comment


def build_review(
    report: Report,
    run_id: str,
    diff: str,
    head_sha: str | None,
    *,
    roster: RosterStatus = (),
) -> BuiltReview:
    """Compose the create-review payload: a summary body + one inline comment per anchor group.
    Findings whose line isn't in the diff are listed in the body instead (never dropped).

    `head_sha` is the commit the diff was taken against; sent as `commit_id` so comments anchor to
    the reviewed commit (not a commit pushed later). If it's unknown (None) — the head couldn't be
    captured or moved mid-fetch — we can't anchor safely: inline comments would bind to GitHub's
    *latest* commit, whose diff may differ. So we then post NO inline comments and route every
    finding into the summary body instead."""
    groups: dict[tuple[str, int, int], list[MergedFinding]] = {}
    unanchored: list[MergedFinding] = []
    note: str | None = None
    if head_sha is None:
        unanchored = list(report.findings)  # no known commit to anchor against -> summarize all
        note = (
            "_aeview couldn't pin the exact reviewed commit (the PR head moved or couldn't be read "
            "during the review), so findings are summarized here rather than anchored to the diff._"
        )
    else:
        index = _diff_anchorable_lines(diff)
        for f in report.findings:
            span = _anchor_span(f.location, index)
            if span is None:
                unanchored.append(f)
            else:
                # Same-span findings share one comment/thread (the API can't thread siblings).
                groups.setdefault((f.location.file, *span), []).append(f)
    comments = [
        _inline_comment(path, first, last, fs, run_id) for (path, first, last), fs in groups.items()
    ]
    payload: dict[str, object] = {
        "event": "COMMENT",
        "body": _review_body(report, run_id, unanchored, roster, note=note),
    }
    # Same `is None` test as the anchoring branch above, so the two decisions can't drift: a known
    # head means we both anchored inline and pin commit_id; None means neither.
    if head_sha is not None:
        payload["commit_id"] = head_sha
    if comments:
        payload["comments"] = comments
    return BuiltReview(
        payload=payload,
        inline_findings=sum(len(fs) for fs in groups.values()),
        body_findings=len(unanchored),
    )


# --- posting --------------------------------------------------------------------------


def _gh_api(
    method: str,
    endpoint: str,
    payload: Mapping[str, object] | None,
    cwd: Path,
    timeout: float = _GH_TIMEOUT_S,
) -> ProcResult:
    # The JSON body is encoded here (the single JSON boundary) and piped on stdin, so a comments[]
    # array (which `-f` flags can't express) and large bodies both pass cleanly. Bounded by a
    # timeout so a stalled gh can't hang the CLI after the run already produced its report.
    args = ["gh", "api", "--method", method, *_GH_API_HEADERS, endpoint]
    if payload is None:
        return run_sync(args, cwd=cwd, timeout=timeout)
    return run_sync(
        [*args, "--input", "-"], cwd=cwd, input_text=json.dumps(payload), timeout=timeout
    )


def _html_url(stdout: str, fallback: str) -> str:
    try:
        return json.loads(stdout).get("html_url") or fallback
    except json.JSONDecodeError, AttributeError:
        return fallback


def post_review(
    target: PrTarget,
    report: Report,
    run_id: str,
    diff: str,
    head_sha: str | None,
    cwd: Path,
    *,
    roster: RosterStatus = (),
) -> PostResult:
    """Post the merged report as one PR review. `head_sha` is the commit the reviewed diff was taken
    against — comments anchor to it. On API rejection, fall back to a single top-level comment with
    every finding so nothing is lost. Raises GitHubError only if both posts fail."""
    built = build_review(report, run_id, diff, head_sha, roster=roster)
    res = _gh_api(
        "POST",
        f"repos/{target.owner}/{target.repo}/pulls/{target.number}/reviews",
        built.payload,
        cwd,
    )
    if res.returncode == 0:
        return PostResult(
            url=_html_url(res.stdout, target.url),
            inline=built.inline_findings,
            in_body=built.body_findings,
        )
    reason = res.stderr.strip() or f"gh exited {res.returncode}"
    # Only a definite 4xx rejection proves the review was NOT created (safe to salvage via a
    # comment). Anything else (timeout, 5xx, network) is ambiguous — the review may exist, so a
    # fallback would double-post; raise instead.
    if not _CLIENT_REJECTION.search(res.stderr):
        raise AmbiguousPostError(
            f"posting the review to PR #{target.number} failed ambiguously ({reason}); it may or "
            "may not have been created — check the PR before re-running --post-comments."
        )

    # Definite rejection: rather than drop the findings, post them all as one top-level PR comment
    # (an issue comment) and tell the caller we degraded.
    note = (
        "_aeview could not attach inline comments to the diff "
        f"({reason}); all findings are listed below._"
    )
    body = _review_body(report, run_id, report.findings, roster, note=note)
    cres = _gh_api(
        "POST",
        f"repos/{target.owner}/{target.repo}/issues/{target.number}/comments",
        {"body": body},
        cwd,
    )
    if cres.returncode != 0:
        # Same rule as the review POST: only a definite 4xx proves the fallback wasn't created.
        error = GitHubError if _CLIENT_REJECTION.search(cres.stderr) else AmbiguousPostError
        raise error(
            f"failed to post the review to PR #{target.number} ({reason}); "
            f"the fallback comment also failed ({cres.stderr.strip() or cres.returncode})."
        )
    return PostResult(
        url=_html_url(cres.stdout, target.url),
        inline=0,
        in_body=len(report.findings),
        fallback_reason=reason,
    )


# --- the live progress comment ----------------------------------------------------------
#
# A review can't be "in progress" on GitHub: inline comments only attach to a *pending* review,
# which nobody else can see, and a submitted one can't gain inline comments (verified against the
# API). So live progress is a separate issue comment, edited as reviews finish and deleted once the
# real review lands. One comment per run (never shared), so concurrent runs on a PR can't clobber
# each other's edits; the hidden marker names the run for humans and tooling.

ProgressPhase = Literal["reviewing", "merging", "interrupted", "failed"]

_PROGRESS_MARKER = "<!-- aeview:progress run={run_id} -->"
_PHASE_HEADLINE: dict[ProgressPhase, str] = {
    "reviewing": "⏳ **reviewing…**",
    "merging": "⏳ **merging findings…**",
    "interrupted": "⚠️ **interrupted**",
    "failed": "❌ **failed**",
}
_BAR_WIDTH = 20
# Edits are cosmetic and run while the panel is live; keep a stalled gh from holding up the run
# (a Ctrl-C waits on an in-flight edit) for as long as the final post may take.
_PROGRESS_TIMEOUT_S = 15
_NOT_FOUND = re.compile(r"HTTP 404")
_FORBIDDEN = re.compile(r"HTTP 403")
_HIDE_AS_OUTDATED = (
    "mutation($id: ID!) { minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) "
    "{ minimizedComment { isMinimized } } }"
)


def _cell(text: str) -> str:
    return _sanitize(text).replace("|", "\\|").replace("\n", " ")


def _status_cell(r: ReviewResult | None, *, live: bool) -> str:
    if r is None or r.status == "pending":
        return "⏸️ pending"
    if r.status == "running":
        # Once the run is over, nothing is still running — it was cut off mid-review.
        return "🔄 running" if live else "⚠️ interrupted"
    if r.status == "failed":
        return "❌ failed"
    n = len(r.findings)
    return f"✅ done · {n} finding{'' if n == 1 else 's'}"


def roster_status(
    roster: Sequence[RosterEntry], reviews: Mapping[str, ReviewResult]
) -> RosterStatus:
    """Pair each roster entry with its review result (None = not started yet)."""
    return [(e, reviews.get(e.id)) for e in roster]


def _roster_table(roster: RosterStatus, *, live: bool) -> str:
    rows = ["| Reviewer | Harness | Model | Status |", "|---|---|---|---|"]
    for e, result in roster:
        model = f"{e.model} ({e.thinking})" if e.thinking else e.model
        rows.append(
            f"| {_cell(e.reviewer)} | {_cell(e.harness)} | {_cell(model)} "
            f"| {_status_cell(result, live=live)} |"
        )
    return "\n".join(rows)


def render_progress(
    run_id: str,
    roster: list[RosterEntry],
    reviews: Mapping[str, ReviewResult],
    phase: ProgressPhase,
    head_sha: str | None,
    note: str | None = None,
) -> str:
    total = len(roster)
    finished = sum(
        1 for e in roster if (r := reviews.get(e.id)) is not None and r.status in ("done", "failed")
    )
    filled = round(_BAR_WIDTH * finished / total) if total else _BAR_WIDTH
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
    table = _roster_table(roster_status(roster, reviews), live=phase in ("reviewing", "merging"))
    if phase == "interrupted" and note is None:
        note = f"Resume with `aeview resume {run_id}`."
    meta = [f"run `{run_id}`"]
    if head_sha:
        meta.append(f"commit `{head_sha[:12]}`")
    if phase in ("reviewing", "merging"):
        meta.append("replaced by the review when the run finishes")
    parts = [
        f"{_SUMMARY_BADGE} — {_PHASE_HEADLINE[phase]}",
        f"`{bar}` {finished}/{total} reviews finished",
        table,
        note,
        "<sub>" + " · ".join(meta) + "</sub>",
        _PROGRESS_MARKER.format(run_id=run_id),
    ]
    return "\n\n".join(p for p in parts if p)


def _comment_endpoint(target: PrTarget, comment_id: int) -> str:
    return f"repos/{target.owner}/{target.repo}/issues/comments/{comment_id}"


def _edit_comment(target: PrTarget, comment_id: int, body: str, cwd: Path) -> ProcResult:
    return _gh_api(
        "PATCH",
        _comment_endpoint(target, comment_id),
        {"body": body},
        cwd,
        timeout=_PROGRESS_TIMEOUT_S,
    )


def delete_comment(target: PrTarget, comment_id: int, cwd: Path) -> bool:
    """Remove an issue comment; True once it no longer shows: deleted, already gone (404), or — if
    GitHub refuses the delete (403) — hidden as outdated instead. A transient failure is False, so
    the next run's sweep retries."""
    res = _gh_api("DELETE", _comment_endpoint(target, comment_id), None, cwd)
    if res.returncode == 0 or _NOT_FOUND.search(res.stderr):
        return True
    return bool(_FORBIDDEN.search(res.stderr)) and _hide_as_outdated(target, comment_id, cwd)


def _hide_as_outdated(target: PrTarget, comment_id: int, cwd: Path) -> bool:
    # Minimizing is GraphQL-only and keyed by node id, which the REST comment carries.
    got = _gh_api("GET", _comment_endpoint(target, comment_id), None, cwd)
    try:
        node_id = json.loads(got.stdout).get("node_id") if got.returncode == 0 else None
    except json.JSONDecodeError, AttributeError:
        node_id = None
    if not isinstance(node_id, str):
        return False
    payload = {"query": _HIDE_AS_OUTDATED, "variables": {"id": node_id}}
    res = _gh_api("POST", "graphql", payload, cwd)
    try:
        hidden = json.loads(res.stdout)["data"]["minimizeComment"]["minimizedComment"]
    except json.JSONDecodeError, KeyError, TypeError:
        return False
    return res.returncode == 0 and isinstance(hidden, dict) and hidden.get("isMinimized") is True


@dataclass(slots=True)
class ProgressComment:
    """One run's live progress comment. The run's orchestrator is its sole writer. Edits from the
    fan-out are coalesced: while one PATCH is in flight, further updates just mark the body dirty,
    so a burst of finishing reviews costs one more edit, not one each. Edits are best-effort — the
    first failure is kept in `error` for the CLI to surface and never interrupts the run."""

    target: PrTarget
    run_id: str
    roster: list[RosterEntry]
    head_sha: str | None
    cwd: Path
    reviews: dict[str, ReviewResult] = field(default_factory=dict)
    comment_id: int = 0
    phase: ProgressPhase = "reviewing"
    note: str | None = None
    error: str | None = None
    _flush: asyncio.Task[None] | None = field(default=None, init=False)
    _dirty: bool = field(default=False, init=False)

    def body(self) -> str:
        return render_progress(
            self.run_id, self.roster, self.reviews, self.phase, self.head_sha, self.note
        )

    def record(self, result: ReviewResult) -> None:
        """fan_out's on_update hook: runs on the event loop, never blocks, never raises."""
        self.reviews[result.id] = result
        self._schedule()

    def set_phase(self, phase: ProgressPhase) -> None:
        self.phase = phase
        self._schedule()

    def _schedule(self) -> None:
        self._dirty = True
        if self._flush is None or self._flush.done():
            self._flush = asyncio.get_running_loop().create_task(self._run_flush())

    async def _run_flush(self) -> None:
        # In a worker thread (not run_async): on Ctrl-C asyncio.run joins the executor before
        # returning, so an in-flight edit always lands *before* the "interrupted" edit, never after.
        while self._dirty:
            self._dirty = False
            res = await asyncio.to_thread(
                _edit_comment, self.target, self.comment_id, self.body(), self.cwd
            )
            self._note_failure(res)

    async def drain(self) -> None:
        if self._flush is not None:
            await self._flush

    def finish(self, phase: ProgressPhase, note: str | None = None) -> None:
        """Synchronous terminal edit (interrupted/failed), made outside the event loop."""
        self.phase, self.note = phase, note
        self._note_failure(_edit_comment(self.target, self.comment_id, self.body(), self.cwd))

    def _note_failure(self, res: ProcResult) -> None:
        if res.returncode != 0 and self.error is None:
            self.error = res.stderr.strip() or f"gh exited {res.returncode}"


def open_progress(
    target: PrTarget,
    run_id: str,
    roster: list[RosterEntry],
    head_sha: str | None,
    cwd: Path,
    reviews: Mapping[str, ReviewResult] | None = None,
    existing_id: int | None = None,
) -> ProgressComment:
    """Post the run's progress comment — or, on resume, take back `existing_id` by editing it, and
    post a fresh one only if that comment is definitely gone (a 404, e.g. swept while the run was
    interrupted). Any other edit failure keeps the old id: posting a second comment would orphan
    the first, which nothing then tracks. Raises GitHubError if no comment could be posted."""
    progress = ProgressComment(target, run_id, roster, head_sha, cwd, dict(reviews or {}))
    if existing_id is not None:
        progress.comment_id = existing_id
        res = _edit_comment(target, existing_id, progress.body(), cwd)
        if not _NOT_FOUND.search(res.stderr) or res.returncode == 0:
            progress._note_failure(res)
            return progress
    res = _gh_api(
        "POST",
        f"repos/{target.owner}/{target.repo}/issues/{target.number}/comments",
        {"body": progress.body()},
        cwd,
    )
    if res.returncode != 0:
        detail = res.stderr.strip() or f"gh exited {res.returncode}"
        raise GitHubError(f"could not post the progress comment to PR #{target.number} ({detail})")
    try:
        comment_id = json.loads(res.stdout).get("id")
    except json.JSONDecodeError, AttributeError:
        comment_id = None
    if not isinstance(comment_id, int):
        raise GitHubError("`gh api` returned no id for the posted progress comment")
    progress.comment_id = comment_id
    return progress
