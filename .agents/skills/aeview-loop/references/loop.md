# The loop (default — no PR posting)

The review-and-fix loop for `aeview-loop` **without** `--post-comments`. Read
[the convergence reference](convergence.md) first for the stop rule, triage rules, gate-discovery
guidance, and the bounds; this file is the loop itself. It runs until it converges, at most 5 review
cycles.

## Setup: establish the review scope

Decide what the panel reviews: `branch` (the committed change against its base) normally, or
`effective-pr` when the user passed `--no-commit` (the uncommitted work in the tree). Unless
`--no-commit`, commit the change first if it isn't committed yet — the `branch` scope needs it on
the branch to see it.

## Cycle 0: green the gates

Discover the project's hard gates — its tests, linter, and type-checker (from `pyproject.toml` /
`package.json` scripts / `Makefile` / `justfile` / CI workflows — see convergence.md) — and run
them. Fix any failures, and commit the fix **only if there was something to fix** (never under
`--no-commit`). Don't run the panel yet: cycle 0 just gets you to a clean, gate-passing baseline.

## Cycles 1–5: review and fix

Each review cycle, in order:

1. **Run the aeview panel**, always with `--json` (the JSON gate is the reliable contract). A full
   panel takes a few minutes, so **run it as a background task** rather than blocking:

   ```bash
   aeview run --scope branch --json [--reviewers …]      # --scope effective-pr under --no-commit
   ```

   It prints its run id on stderr; let it finish (`aeview status <run-id> --wait`), then read the
   JSON gate. Exit code is the verdict: `0` approve · `1` needs-attention · `2` error; full report
   via `aeview result <run-id>`.
2. **Triage and fix.** **You** decide address vs ignore, filtering out findings premised on context
   the reviewers can't see, and **flagging genuine design or security decisions to the user instead
   of deciding alone**. Fix the actionable ones.
3. **Re-run the hard gates** — a resolution can break them, so gate the fixes before you commit;
   they must pass (fix any breakage first).
4. **Commit** the cycle's fixes (**skip under `--no-commit`**).
5. **Converged?** If the cycle surfaced **no new actionable findings** (not necessarily zero — see
   convergence.md), stop. Otherwise run the next cycle — at most 5 review cycles, then stop and
   report what's open. The user can raise the cap with `--max-cycles`.
