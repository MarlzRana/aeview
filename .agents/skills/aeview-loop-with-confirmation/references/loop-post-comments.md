# The loop with `--post-comments` (confirm via PR threads)

The review-and-fix loop for `aeview-loop-with-confirmation` **when `--post-comments` is passed**.
aeview posts each cycle's review onto the GitHub PR, you (the implementer) leave a recommendation on
each finding's thread, and **the human replies in those threads to make the call** — the PR threads
*are* the confirmation surface, so **AskUserQuestion is not used**. Read
[the convergence reference](convergence.md) first for convergence under confirmation and how to
surface context the reviewers can't see; this file is the loop itself. It runs until it converges, at
most 5 review cycles.

## Setup: establish the PR

`--post-comments` reviews the PR's *pushed* diff, so:

- **Reject `--no-commit`** — it can't run against uncommitted work. If both are passed, stop and say
  so.
- **Scope is `pr`** for every panel run (not `branch` / `effective-pr`).
- **Ensure an open PR** — aeview errors without one. If there isn't one yet, commit the change, push
  the branch, and open a PR (`gh pr create`).

## Cycle 0: green the gates

Discover the project's hard gates — its tests, linter, and type-checker (from `pyproject.toml` /
`package.json` scripts / `Makefile` / `justfile` / CI workflows — see convergence.md) — and run
them. Fix any failures, then commit **and push** the fix — but only if there was something to fix.
Don't run the panel yet: cycle 0 just gets the PR to a clean, gate-passing baseline.

## Each cycle: review → recommend → hand off to the human

1. **Run the aeview panel** against the PR:

   ```bash
   aeview run --scope pr --json --post-comments [--reviewers …]
   ```

   It posts one fresh review (summary + one inline comment per finding) and prints the review URL on
   stderr; **note the run id** (in the gate / on stderr). Let it finish (`aeview status <run-id>
   --wait`).
2. **Recommend on each thread.** For each finding this run raised — **skipping any the human already
   decided in an earlier cycle** — post a recommendation reply on its thread (see *Replying to the
   PR* below): your suggested resolution and why, or **Ignore** with the reason when it's premised on
   context the reviewers can't see (single user, unreleased, migration declined, …). You recommend;
   you do not decide.
3. **Hand off, then stop.** Tell the user the review is on PR #<N> (link), that you've recommended on
   each thread, and that they should reply in the threads with their decision, then say continue.
   **End the turn — do not fix, commit, or loop yet.**

## On resume: read the human's decisions, then act

When the user comes back (says continue / re-invokes):

1. **Re-read the threads:** `gh api repos/{owner}/{repo}/pulls/<N>/comments --paginate`, filtered to
   the latest aeview review's `<!-- aeview:finding run=<run-id> … -->` marker. Within a thread, your
   replies carry `<!-- aeview-loop:reply -->`; **any reply without it is the human's decision** (both
   are authored by the same `gh` account, so the marker is the only reliable signal).
2. **Apply each decision** the human gave: make the fix they asked for, or record an Ignore with
   their reason. Post a short closing reply (`👤 **implementer**` + the marker) noting what you did.
3. **Findings the human didn't answer:** don't act on them — tell the user which threads still need a
   reply and wait. Nothing changes without the human's say-so.
4. **Re-run the hard gates** (a fix can break them), then **commit and push** — so the next cycle's
   `--scope pr` panel reviews the fixes.
5. **Converged?** If the cycle surfaced **no new findings the human elects to fix**, stop. Otherwise
   run the next cycle — at most 5 review cycles, then stop and report what's open. The user can raise
   the cap with `--max-cycles`.

## Replying to the PR

To find the finding comments: `gh api repos/{owner}/{repo}/pulls/<N>/comments --paginate` (`gh` fills
`{owner}`/`{repo}`), filtered to this run's `<!-- aeview:finding run=<run-id> … -->` marker. Reply to
a finding's top-level comment with:

```bash
gh api --method POST repos/{owner}/{repo}/pulls/<N>/comments/<comment-id>/replies -f body='…'
```

(for a multi-line body, pipe JSON via `--input -` — a bare shell `echo` mangles newlines). Open the
body with `👤 **implementer**` and end it with a hidden `<!-- aeview-loop:reply -->` marker.

Any finding aeview couldn't anchor is in the **summary comment**, not an inline thread — surface
those to the human explicitly, since there's no thread to reply in.
