---
name: aeview-loop
description: After you and the user have planned and implemented a change, loop with the aeview reviewer panel until it converges — running the project's gates, reviewing with the panel, and fixing the findings each cycle. Use when a change is already written and the user wants it reviewed and fixed until it passes the gates and the panel finds no new issues — "review until clean" or "loop until the panel is happy". It fixes what the panel finds; it does not build the change from scratch. For a one-shot review with no fixes, use the aeview skill instead.
argument-hint: '[--reviewers a,b] [--no-commit] [--max-cycles n] [what to build or fix]'
---

# aeview-loop

You and the user have already planned and implemented the change. This skill loops with the
`aeview` reviewer panel until it converges — you run the gates, run the panel, triage the findings,
and **fix** them. Unlike the `aeview` skill (review-only), this skill changes code; but the change
is already written before you start, so here you fix what the panel finds rather than build from
scratch.

Raw arguments: `$ARGUMENTS`

- Freeform text is context for the review — the scope/files to focus on, or a note on what the
  change does.
- `--reviewers a,b` and other `aeview run` flags pass straight through to the panel.
- `--no-commit` — don't commit between cycles; work stays in the tree (changes the review scope).
- `--max-cycles <n>` — override the default cap of 5 review cycles (the loop stops as soon as it
  converges regardless). The user can also name this in plain language ("at most 3 cycles").

## The loop (runs until it converges — at most 5 review cycles)

Read [the convergence reference](references/convergence.md) once — it defines convergence, the
triage rules, the gate-discovery guidance, and the bounds. Then run the loop.

### Setup: establish the review scope

Decide what the panel reviews: `branch` (the committed change against its base) normally, or
`effective-pr` when the user passed `--no-commit` (the uncommitted work in the tree). Unless
`--no-commit`, commit the change first if it isn't committed yet — the `branch` scope needs it on
the branch to see it.

### Cycle 0: green the gates

Discover the project's hard gates — its tests, linter, and type-checker (from `pyproject.toml` /
`package.json` scripts / `Makefile` / `justfile` / CI workflows — see the reference) — and run them.
Fix any failures, and commit the fix **only if there was something to fix** (never under
`--no-commit`). Don't run the panel yet: cycle 0 just gets you to a clean, gate-passing baseline.

### Cycles 1–5: review and fix

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
   the reference), stop. Otherwise run the next cycle — at most 5 review cycles, then stop and
   report what's open. The user can raise the cap with `--max-cycles`.

## Required summary

When the loop ends, give the user a summary:

- **Addressed** — each finding you fixed, and what changed.
- **Ignored** — each finding you didn't, **with the reason**.
- The cycle count and the outcome (converged, or stopped at the cap with N open).
