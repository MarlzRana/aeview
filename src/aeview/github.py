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
from dataclasses import dataclass
from pathlib import Path

from .process import run_sync
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


class GitHubError(Exception):
    """Raised when the PR target can't be resolved or posting fails irrecoverably."""


@dataclass(slots=True)
class PrTarget:
    """The open PR a review posts to (resolved before the fan-out, so a missing PR fails fast)."""

    number: int
    head_sha: str  # anchors comments to the PR head; stale if a commit lands mid-run
    url: str


@dataclass(slots=True)
class PostResult:
    """What post_review did, for the CLI's stderr status line."""

    url: str
    inline: int  # findings posted as inline comments
    in_body: int  # findings listed in the summary body (unanchored, or all of them on fallback)
    fell_back: bool = False  # the review API rejected the batch; posted one summary comment instead
    reason: str | None = None


# --- PR target resolution -------------------------------------------------------------


def resolve_pr_target(cwd: Path, value: str | None) -> PrTarget:
    """Resolve the open PR to post to. `value` is the --scope pr value (a PR number, or None for the
    current branch's PR). Raises GitHubError if gh fails, no PR exists, or the PR isn't open — the
    run must not spend a whole fan-out only to discover there's nowhere to post."""
    args = ["gh", "pr", "view"]
    if value:
        args.append(value)
    args += ["--json", "number,state,headRefOid,url"]
    res = run_sync(args, cwd=cwd)
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
    return PrTarget(number=number, head_sha=head_sha, url=url)


# --- diff line index (which lines can carry an inline comment) ------------------------

_HUNK = re.compile(r"^@@ -(?:\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _diff_added_lines(diff: str) -> dict[str, set[int]]:
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
                path = line[4:].split("\t", 1)[0].removeprefix("b/")
                current = None if path == "/dev/null" else index.setdefault(path, set())
            continue
        if current is None:
            continue
        if line.startswith("+"):  # an added line: a new-file line a comment can anchor to
            current.add(new_no)
            new_no += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue  # a deletion (old-file only) or the "\ No newline" marker: no new-file line
        else:  # context line: present in the new file too
            current.add(new_no)
            new_no += 1
    return index


def _anchor(loc: Location, index: dict[str, set[int]]) -> int | None:
    """The new-file line an inline comment can anchor to (always RIGHT side), or None if the
    finding's line isn't in the diff (then it falls to the summary). Findings always cite
    post-change lines, so a coincidental match against an old-file line would mis-place the comment
    — hence RIGHT only."""
    lines = index.get(loc.file)
    if lines is None or loc.line_start not in lines:
        return None
    return loc.line_start


# --- review payload -------------------------------------------------------------------


@dataclass(slots=True)
class BuiltReview:
    payload: dict
    inline_findings: int
    body_findings: int


def _location_md(loc: Location) -> str:
    loc_str = f"{loc.file}:{loc.line_start}"
    if loc.line_end != loc.line_start:
        loc_str += f"-{loc.line_end}"
    return f"`{loc_str}`"


def _provenance(f: MergedFinding) -> str:
    """One line naming the reviewer(s) that raised this finding (from its merge sources), so it's
    clear which panel member spoke even though the comment is authored by the gh user."""
    reviewers = ", ".join(s.review for s in f.sources) or "—"
    bits = [f"reviewers: {reviewers}", f"confidence {f.confidence:.2f}"]
    if f.agreement > 1:
        bits.append(f"agreement ×{f.agreement}")
    return "<sub>" + " · ".join(bits) + "</sub>"


def _finding_md(f: MergedFinding, run_id: str, *, show_location: bool) -> str:
    """Render one finding. `show_location` is for body-listed (unanchored) findings, where there is
    no inline anchor to convey the file/line; inline comments omit it (the anchor shows it)."""
    head = f"`{f.severity}` · {f.category} — **{f.title}**"
    if show_location:
        head = f"{_location_md(f.location)} · {head}"
    blocks = [
        _BADGE,
        head,
        f.body.strip(),
        f"**Fix:** {f.recommendation.strip()}",
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
    index = _diff_added_lines(diff)
    groups: dict[tuple[str, int], list[MergedFinding]] = {}
    unanchored: list[MergedFinding] = []
    for f in report.findings:
        line = _anchor(f.location, index)
        if line is None:
            unanchored.append(f)
        else:
            groups.setdefault((f.location.file, line), []).append(f)
    comments = [
        {
            "path": path,
            "line": line,
            "side": "RIGHT",  # findings cite post-change lines; we only anchor on the new file
            # Same-line findings share one comment (one thread): the API can't thread siblings.
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


def _gh_api_post(endpoint: str, body_json: str, cwd: Path):
    # {owner}/{repo} are filled by gh from the repo at cwd; the JSON body is piped on stdin so a
    # comments[] array (which `-f` flags can't express) and large bodies both pass cleanly.
    return run_sync(
        ["gh", "api", "--method", "POST", *_GH_API_HEADERS, endpoint, "--input", "-"],
        cwd=cwd,
        input_text=body_json,
    )


def _html_url(stdout: str, fallback: str) -> str:
    try:
        return json.loads(stdout).get("html_url") or fallback
    except (json.JSONDecodeError, AttributeError):
        return fallback


def post_review(target: PrTarget, report: Report, run_id: str, diff: str, cwd: Path) -> PostResult:
    """Post the merged report as one PR review. On API rejection, fall back to a single top-level
    comment with every finding so nothing is lost. Raises GitHubError only if both posts fail."""
    built = build_review(report, run_id, target.head_sha, diff)
    res = _gh_api_post(
        f"repos/{{owner}}/{{repo}}/pulls/{target.number}/reviews",
        json.dumps(built.payload),
        cwd,
    )
    if res.returncode == 0:
        return PostResult(
            url=_html_url(res.stdout, target.url),
            inline=built.inline_findings,
            in_body=built.body_findings,
        )

    # The create-review call is all-or-nothing; rather than drop the findings, post them all as one
    # top-level PR comment (an issue comment) and tell the caller we degraded.
    reason = res.stderr.strip() or f"gh exited {res.returncode}"
    note = (
        "_aeview could not attach inline comments to the diff "
        f"({reason}); all findings are listed below._"
    )
    body = _review_body(report, run_id, list(report.findings), note=note)
    cres = _gh_api_post(
        f"repos/{{owner}}/{{repo}}/issues/{target.number}/comments",
        json.dumps({"body": body}),
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
        fell_back=True,
        reason=reason,
    )
