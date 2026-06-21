---
name: aeview-loop-with-confirmation
description: Like aeview-loop — run after you and the user have planned and implemented a change — but it pauses every cycle to confirm each reviewer finding with the user before acting. For each finding it presents the issue, tailored resolution options (always including Ignore) and its recommendation via AskUserQuestion, then applies the chosen option. Use when you want to approve every fix the loop makes; for the autonomous version use aeview-loop.
argument-hint: '[--reviewers a,b] [--no-commit] [--min-cycles n] [--max-cycles n] [what to build or fix]'
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
- `--no-commit` — don't commit between cycles; work stays in the tree (changes the review scope, see
  step 4).
- `--min-cycles <n>` / `--max-cycles <n>` — override the default loop bounds (min 2, max 5). The user
  can also name these in plain language ("at least 1 cycle", "at most 3").

## The loop (default: min 2 / max 5 cycles — see "Bounds")

Read [the convergence reference](references/convergence.md) once — it defines convergence under
confirmation, how to surface context the reviewers can't see, the gate-discovery guidance, and the
bounds. Then run the loop:

### 1. Start from the implemented change

Cycle 1: the change is already implemented — you built it with the user before invoking this skill,
so go straight to the gates. Later cycles: the user's chosen fixes from the previous cycle were
applied at the end of step 5, so this cycle just re-gates and re-reviews them.

### 2. Run the project's hard gates

Discover the project's own tests, linter, and type-checker (from `pyproject.toml` / `package.json`
scripts / `Makefile` / `justfile` / CI workflows — see the reference) and run them. **They must
pass before you continue**; if none are discoverable or they're ambiguous, ask the user which
commands count. These gates are required every cycle, independent of the panel.

### 3. Commit the cycle's work

Commit with a conventional message (`feat:` / `fix:` / `refactor:` / `nit:`, imperative mood).
**Skip this step if the user passed `--no-commit`.**

### 4. Run the aeview panel (prefer the background)

Always pass `--json` (the JSON gate is the reliable contract). A full panel takes a few minutes, so
**run it as a background task** rather than blocking:

```bash
aeview run --scope branch --json [--reviewers …]
```

Review the change against its base. With `--no-commit` there's uncommitted work, so review that
instead:

```bash
aeview run --scope effective-pr --json [--reviewers …]
```

It prints its run id on stderr; let it finish (`aeview status <run-id> --wait`), then read the JSON
gate. Exit code is the verdict: `0` approve · `1` needs-attention · `2` error; full report via
`aeview result <run-id>`.

### 5. Confirm every finding with the user

This is the heart of this skill. Do **not** decide on your own. For each finding the panel reports
this cycle — **skipping any the user already decided on in an earlier cycle** (see the reference) —
build an AskUserQuestion question:

- **header** — a short tag for the finding (e.g. `sql-injection`, `missing-test`).
- **question** — state the finding plainly: severity, `file:line`, title, and the reviewer's
  recommendation. Then ask how to resolve it.
- **options** — 1–3 concrete resolutions tailored to *that* finding (the reviewer's suggested fix
  and any sensible alternative), **plus an always-present "Ignore"**. Put your recommended option
  first and mark it "(recommended)". When the finding is premised on context the reviewers can't see
  (single user, unreleased, migration declined, …), recommend **Ignore** and say why in its
  description — surface it, don't pre-filter it (see the reference).

Batch up to **4 findings per AskUserQuestion call**; if there are more, ask in successive calls.
Apply each choice: a fix option → make that change; **Ignore** → record it (with the user's reason)
for the summary and don't ask about it again. Then go back to step 1.

### Bounds — when to stop

Run **at least the minimum and at most the maximum** number of cycles — **default min 2, max 5** —
stopping as soon as a cycle surfaces **no new findings the user elects to fix**. If the user gave
`--min-cycles` / `--max-cycles` (or named the values in their request), use those instead of the
defaults.

## Required summary

When the loop ends, give the user a summary:

- **Addressed** — each finding the user chose to fix, and what changed.
- **Ignored** — each finding the user chose to ignore, with their reason.
- The cycle count and the outcome (converged, stopped at the cap with N open, or stopped on request).
