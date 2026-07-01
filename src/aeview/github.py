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

Threading: each run is its own review, so re-running leaves a fresh thread per finding even on a
line a previous run already commented on — the series of reviews is the PR's audit trail. Within one
review, findings sharing an anchor are merged into one comment (the API can't thread siblings that
don't have ids yet).

All `gh` interaction for posting is funnelled through here, keeping merge.py/report.py I/O-free.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .process import ProcResult, run_sync
from .report import report_verdict_label
from .schema import Location, MergedFinding, Report

# Visible badge so a human reading the PR can tell aeview spoke (the comment is authored by the
# gh-authenticated user, not a distinct bot account); the hidden marker is the machine signal.
_BADGE = "🤖 **aeview** (automated review panel)"
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


@dataclass(slots=True)
class PrTarget:
    """The open PR a review posts to (resolved before the fan-out, so a missing PR fails fast)."""

    number: int
    head_sha: str  # anchors comments to the PR head; stale if a commit lands mid-run
    url: str
    # The PR's own repo; used to address the API explicitly instead of gh's cwd-derived
    # {owner}/{repo} (which would target a local fork, not the upstream the PR lives on).
    owner: str = "{owner}"
    repo: str = "{repo}"


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
    args += ["--json", "number,state,headRefOid,url"]
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
    number, head_sha, url = data.get("number"), data.get("headRefOid"), data.get("url")
    if not (isinstance(number, int) and isinstance(head_sha, str) and isinstance(url, str)):
        raise GitHubError("`gh pr view` returned an incomplete PR record (number/headRefOid/url).")
    # Address the PR's own repo (parsed from its url), not gh's cwd-derived {owner}/{repo}.
    owner, repo = "{owner}", "{repo}"
    if m := _PR_URL.search(url):
        owner, repo = m.group(1), m.group(2)
    return PrTarget(number=number, head_sha=head_sha, url=url, owner=owner, repo=repo)


# --- diff line index (which lines can carry an inline comment) ------------------------

_HUNK = re.compile(r"^@@ -(?:\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _diff_anchorable_lines(diff: str) -> dict[str, set[int]]:
    """Map each changed file to the new-file line numbers a comment can anchor to (additions +
    context). Findings cite post-change lines, so anchoring is RIGHT-side only — an old-file line
    (a deletion) has no new-file number and can't be anchored, so it isn't tracked.

    Header lines (`+++ `) and hunk-body content are told apart by hunk state, not prefix alone: an
    added line whose own text starts with `++ ` reads `+++ ...` in the diff — inside a hunk that's
    content, and treating it as a header would reset the counter and corrupt every later line."""
    index: dict[str, set[int]] = {}
    current: set[int] | None = None
    in_hunk = False
    new_no = 0
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            current, in_hunk = None, False  # next file; its +++ header + counts come before @@
            continue
        if line.startswith("@@"):
            if m := _HUNK.match(line):
                new_no = int(m.group(1))  # the +new hunk start (RIGHT/new-file numbering)
                in_hunk = True
            continue
        if not in_hunk:  # pre-hunk preamble: the index/mode/+++/--- header lines
            if line.startswith("+++ "):
                raw = line[4:].split("\t", 1)[0]
                if raw.startswith('"') and raw.endswith('"'):
                    raw = raw[1:-1]  # git C-quotes paths with spaces/specials; unwrap the quotes
                path = raw.removeprefix("b/")
                current = None if path == "/dev/null" else index.setdefault(path, set())
            continue
        if current is None:
            continue
        if line.startswith("+"):  # an added line: a new-file line a comment can anchor to
            current.add(new_no)
            new_no += 1
        elif line.startswith(("-", "\\")):
            continue  # a deletion (old-file only) or the "\ No newline" marker: no new-file line
        else:  # context line: present in the new file too
            current.add(new_no)
            new_no += 1
    return index


def _anchor_line(loc: Location, index: dict[str, set[int]]) -> int | None:
    """The new-file line an inline comment anchors to (always RIGHT side), or None if the finding's
    start line isn't in the diff — then it falls to the summary. RIGHT-side only: findings cite
    post-change lines, so a coincidental match against an old-file line would mis-place the comment.

    Single-line by design: GitHub requires a multi-line range's two ends to sit in the *same* hunk,
    and a cross-hunk range 422s the *entire* review batch (dumping every inline comment to the
    fallback). Not worth that fragility — we anchor the start line and put the full range in the
    comment body instead (see `_provenance`)."""
    lines = index.get(loc.file)
    if lines is None or loc.line_start not in lines:
        return None
    return loc.line_start


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
    if loc.line_end != loc.line_start:  # anchor is single-line; convey the full span here
        bits.append(f"lines {loc.line_start}–{loc.line_end}")
    if f.agreement > 1:
        bits.append(f"agreement ×{f.agreement}")
    return "<sub>" + " · ".join(bits) + "</sub>"


def _finding_md(f: MergedFinding, run_id: str, *, show_location: bool) -> str:
    """Render one finding. `show_location` is for body-listed (unanchored) findings, where there is
    no inline anchor to convey the file/line; inline comments omit it (the anchor shows it).

    title/body/recommendation are model output derived from an untrusted diff, so they're sanitized
    (mentions/markers) and length-capped before they're posted under the user's account."""
    head = f"`{f.severity}` · {f.category} — **{_sanitize(f.title.strip())}**"
    if show_location:
        head = f"{_location_md(f.location)} · {head}"
    blocks = [
        _BADGE,
        head,
        _clip(_sanitize(f.body.strip()), run_id),
        f"**Fix:** {_clip(_sanitize(f.recommendation.strip()), run_id)}",
        _provenance(f),
        _FINDING_MARKER.format(run_id=run_id, finding_id=f.id),
    ]
    return "\n\n".join(b for b in blocks if b)


def _review_body(
    report: Report, run_id: str, listed: list[MergedFinding], *, note: str | None = None
) -> str:
    """The review's top-level summary. `listed` findings are rendered in full here — the unanchored
    ones for a normal review, or every finding on the fallback path (where `note` explains why)."""
    label = report_verdict_label(report)
    parts = [
        f"{_BADGE} — **{label}**",
        report.summary.strip() or "(no summary)",
    ]
    cov = report.coverage
    if cov.failed:
        parts.append(f"Coverage: {cov.contributed} review(s) contributed, {cov.failed} failed.")
    if note:
        parts.append(note)
    if listed:
        parts.append("#### Findings" if note else "#### Findings not anchored to the diff")
        parts.extend(_finding_md(f, run_id, show_location=True) for f in listed)
    parts.append(_REVIEW_MARKER.format(run_id=run_id))
    return "\n\n".join(parts)


def build_review(report: Report, run_id: str, head_sha: str, diff: str) -> BuiltReview:
    """Compose the create-review payload: a summary body + one inline comment per anchor group.
    Findings whose line isn't in the diff are listed in the body instead (never dropped)."""
    index = _diff_anchorable_lines(diff)
    groups: dict[tuple[str, int], list[MergedFinding]] = {}
    unanchored: list[MergedFinding] = []
    for f in report.findings:
        line = _anchor_line(f.location, index)
        if line is None:
            unanchored.append(f)
        else:
            # Findings on the same line share one comment/thread (the API can't thread siblings).
            groups.setdefault((f.location.file, line), []).append(f)
    comments = [
        {
            "path": path,
            "line": line,
            "side": "RIGHT",  # findings cite post-change lines; we only anchor on the new file
            "body": "\n\n---\n\n".join(_finding_md(f, run_id, show_location=False) for f in fs),
        }
        for (path, line), fs in groups.items()
    ]
    payload: dict[str, object] = {
        "event": "COMMENT",
        "commit_id": head_sha,
        "body": _review_body(report, run_id, unanchored),
    }
    if comments:
        payload["comments"] = comments
    return BuiltReview(
        payload=payload,
        inline_findings=sum(len(fs) for fs in groups.values()),
        body_findings=len(unanchored),
    )


# --- posting --------------------------------------------------------------------------


def _gh_api_post(endpoint: str, payload: Mapping[str, object], cwd: Path) -> ProcResult:
    # The JSON body is encoded here (the single JSON boundary) and piped on stdin, so a comments[]
    # array (which `-f` flags can't express) and large bodies both pass cleanly. Bounded by a
    # timeout so a stalled gh can't hang the CLI after the run already produced its report.
    return run_sync(
        ["gh", "api", "--method", "POST", *_GH_API_HEADERS, endpoint, "--input", "-"],
        cwd=cwd,
        input_text=json.dumps(payload),
        timeout=_GH_TIMEOUT_S,
    )


def _html_url(stdout: str, fallback: str) -> str:
    try:
        return json.loads(stdout).get("html_url") or fallback
    except json.JSONDecodeError, AttributeError:
        return fallback


def post_review(target: PrTarget, report: Report, run_id: str, diff: str, cwd: Path) -> PostResult:
    """Post the merged report as one PR review. On API rejection, fall back to a single top-level
    comment with every finding so nothing is lost. Raises GitHubError only if both posts fail."""
    built = build_review(report, run_id, target.head_sha, diff)
    res = _gh_api_post(
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
        raise GitHubError(
            f"posting the review to PR #{target.number} failed ambiguously ({reason}); it may or "
            "may not have been created — check the PR before re-running --post-comments."
        )

    # Definite rejection: rather than drop the findings, post them all as one top-level PR comment
    # (an issue comment) and tell the caller we degraded.
    note = (
        "_aeview could not attach inline comments to the diff "
        f"({reason}); all findings are listed below._"
    )
    body = _review_body(report, run_id, report.findings, note=note)
    cres = _gh_api_post(
        f"repos/{target.owner}/{target.repo}/issues/{target.number}/comments",
        {"body": body},
        cwd,
    )
    if cres.returncode != 0:
        raise GitHubError(
            f"failed to post the review to PR #{target.number} ({reason}); "
            f"the fallback comment also failed ({cres.stderr.strip() or cres.returncode})."
        )
    return PostResult(
        url=_html_url(cres.stdout, target.url),
        inline=0,
        in_body=len(report.findings),
        fallback_reason=reason,
    )
