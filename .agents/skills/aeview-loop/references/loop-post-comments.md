# The loop with `--post-comments` (posts the review to the PR)

The review-and-fix loop for `aeview-loop` **when `--post-comments` is passed**. aeview posts each
cycle's review onto the GitHub PR, and you (the implementer) reply on every finding's thread with
what you did about it — the series of reviews across cycles is the PR's audit trail. Read
[the convergence reference](convergence.md) first for the stop rule, triage rules, gate-discovery
guidance, and the bounds; this file is the loop itself. It runs until it converges, at most 5 review
cycles.

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

## Cycles 1–5: review, fix, and reply on the PR

Each review cycle, in order:

1. **Run the aeview panel** against the PR:

   ```bash
   aeview run --scope pr --json --post-comments [--reviewers …]
   ```

   On success it posts one fresh review to the PR — a summary comment plus one inline comment per
   finding — and prints the review URL on stderr; the JSON gate on stdout is unchanged. **Note the
   run id** (in the gate / on stderr); you need it to find the comments. Let it finish
   (`aeview status <run-id> --wait`); the exit code is the verdict (`0`/`1`/`2`).
2. **Triage and fix.** **You** decide address vs ignore, filtering out findings premised on context
   the reviewers can't see, and **flagging genuine design or security decisions to the user instead
   of deciding alone**. Fix the actionable ones.
3. **Re-run the hard gates** — a resolution can break them, so gate the fixes before you commit;
   they must pass (fix any breakage first).
4. **Commit and push** the cycle's fixes, so the next cycle's `--scope pr` panel reviews them.
5. **Reply on each finding's thread** with its disposition, so the PR records the outcome — see
   *Replying to the PR* below.
6. **Converged?** If the cycle surfaced **no new actionable findings** (not necessarily zero — see
   convergence.md), stop. Otherwise run the next cycle — at most 5 review cycles, then stop and
   report what's open. The user can raise the cap with `--max-cycles`.

## Replying to the PR

After triaging each cycle, reply on every finding's thread:

1. **List this review's finding comments:**
   `gh api repos/{owner}/{repo}/pulls/<N>/comments --paginate` (`gh` fills `{owner}`/`{repo}`).
   aeview's inline comments carry a hidden `<!-- aeview:finding run=<run-id> id=<finding-id> -->`
   marker — filter to this run's `<run-id>` to get this cycle's threads and map each to its finding.
2. **Reply** to each finding's top-level comment:
   `gh api --method POST repos/{owner}/{repo}/pulls/<N>/comments/<comment-id>/replies -f body='…'`
   (for a multi-line body, pipe JSON via `--input -` — a bare shell `echo` mangles newlines).
3. **Body:** open with `👤 **implementer**`, end with a hidden `<!-- aeview-loop:reply -->` marker,
   and state the disposition — **fixed** (what changed) / **ignored** (+ the reason, e.g. the
   reviewer lacks context) / **deferred**. The marker matters: your comments and the human's are
   authored by the *same* `gh` account, so it's the only reliable way tooling tells them apart.

Any finding **not** on an inline thread — one aeview couldn't anchor, or the whole review when aeview
couldn't pin the reviewed commit (both land in the summary comment) — has no thread to reply in; note
its disposition in your end-of-loop summary instead.
