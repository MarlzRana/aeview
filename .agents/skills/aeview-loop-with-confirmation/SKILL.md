---
name: aeview-loop-with-confirmation
description: Like aeview-loop — run after you and the user have planned and implemented a change — but it pauses every cycle to confirm each reviewer finding with the user before acting. For each finding it presents the issue, tailored resolution options (always including Ignore) and its recommendation via AskUserQuestion, then applies the chosen option. Use when you want to approve every fix the loop makes; for the autonomous version use aeview-loop.
argument-hint: '[--reviewers a,b] [--no-commit] [--max-cycles n] [what to build or fix]'
disable-model-invocation: true
---

# aeview-loop-with-confirmation

Like the `aeview-loop` skill — run after you and the user have planned and implemented a change,
then loop with the `aeview` reviewer panel until it converges — **but the user confirms every
finding before anything is fixed or ignored**. Each cycle, the findings go to the user via
AskUserQuestion, with tailored resolution options and your recommendation; you act only on what they
pick. You run the gates, run the panel, and apply the chosen resolutions — the change is already
written before you start, so you fix what the panel finds rather than build from scratch. You do
**not** triage on your own: you recommend, the user decides.

Raw arguments: `$ARGUMENTS`

- Freeform text is context for the review — the scope/files to focus on, or a note on what the
  change does.
- `--reviewers a,b` and other `aeview run` flags pass straight through to the panel.
- `--no-commit` — don't commit between cycles; work stays in the tree (changes the review scope).
- `--max-cycles <n>` — override the default cap of 5 review cycles (the loop stops as soon as it
  converges regardless). The user can also name this in plain language ("at most 3 cycles").

## The loop (runs until it converges — at most 5 review cycles)

Read [the convergence reference](references/convergence.md) once — it defines convergence under
confirmation, how to surface context the reviewers can't see, the gate-discovery guidance, and the
bounds. Then run the loop.

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

### Cycles 1–5: review and confirm

Each review cycle, in order:

1. **Run the aeview panel**, always with `--json` (the JSON gate is the reliable contract). A full
   panel takes a few minutes, so **run it as a background task** rather than blocking:

   ```bash
   aeview run --scope branch --json [--reviewers …]      # --scope effective-pr under --no-commit
   ```

   It prints its run id on stderr; let it finish (`aeview status <run-id> --wait`), then read the
   JSON gate. Exit code is the verdict: `0` approve · `1` needs-attention · `2` error; full report
   via `aeview result <run-id>`.
2. **Confirm every finding with the user** — the heart of this skill; do **not** decide on your own.
   For each finding the panel reports this cycle (**skip any the user already decided on in an
   earlier cycle** — see the reference), build an AskUserQuestion question:
   - **header** — a short tag for the finding (e.g. `sql-injection`, `missing-test`).
   - **question** — state the finding plainly: severity, `file:line`, title, and the reviewer's
     recommendation. Then ask how to resolve it.
   - **options** — 1–3 concrete resolutions tailored to *that* finding (the reviewer's suggested fix
     and any sensible alternative), **plus an always-present "Ignore"**. Put your recommended option
     first and mark it "(recommended)". When the finding is premised on context the reviewers can't
     see (single user, unreleased, migration declined, …), recommend **Ignore** and say why in its
     description — surface it, don't pre-filter it (see the reference).

   Batch up to **4 findings per AskUserQuestion call**; ask in successive calls if there are more.
   Apply each choice: a fix option → make that change; **Ignore** → record it (with the user's
   reason) for the summary and don't ask about it again.
3. **Re-run the hard gates** — a resolution can break them, so gate the fixes before you commit;
   they must pass (fix any breakage first).
4. **Commit** the cycle's fixes (**skip under `--no-commit`**).
5. **Converged?** If the cycle surfaced **no new findings the user elects to fix**, stop. Otherwise
   run the next cycle — at most 5 review cycles, then stop and report what's open. The user can
   raise the cap with `--max-cycles`.

## Required summary

When the loop ends, give the user a summary:

- **Addressed** — each finding the user chose to fix, and what changed.
- **Ignored** — each finding the user chose to ignore, with their reason.
- The cycle count and the outcome (converged, stopped at the cap with N open, or stopped on request).
