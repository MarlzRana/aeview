---
name: aeview-loop
description: After you and the user have planned and implemented a change, loop with the aeview reviewer panel until it converges — running the project's gates, reviewing with the panel, and fixing the findings each cycle. Use when a change is already written and the user wants it reviewed and fixed until it passes the gates and the panel finds no new issues — "review until clean" or "loop until the panel is happy". It fixes what the panel finds; it does not build the change from scratch. For a one-shot review with no fixes, use the aeview skill instead.
argument-hint: '[--reviewers a,b] [--no-commit] [--min-cycles n] [--max-cycles n] [what to build or fix]'
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
- `--no-commit` — don't commit between cycles; work stays in the tree (changes the review scope, see
  step 4).
- `--min-cycles <n>` / `--max-cycles <n>` — override the default loop bounds (min 2, max 5). The user
  can also name these in plain language ("at least 1 cycle", "at most 3").

## The loop (default: min 2 / max 5 cycles)

Read [the convergence reference](references/convergence.md) once — it defines convergence, the
triage rules, the gate-discovery guidance, and the bounds. Then run the loop:

### 1. Start from the implemented change

Cycle 1: the change is already implemented — you built it with the user before invoking this skill,
so go straight to the gates. Later cycles: the previous panel's fixes were applied in step 5, so
this cycle just re-gates and re-reviews them.

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

### 5. Triage and fix

Triage every finding per the reference — **you** decide address vs ignore, filtering out findings
premised on context the reviewers can't see, and **flagging genuine design or security decisions to
the user instead of deciding alone**. Fix the actionable ones, then go back to step 1.

### Stop when converged (or at the cap)

Run **at least the minimum and at most the maximum** number of cycles — **default min 2, max 5** —
stopping as soon as a cycle surfaces **no new actionable findings** (not necessarily zero). If the
user gave `--min-cycles` / `--max-cycles` (or named the values in their request), use those instead
of the defaults.

## Required summary

When the loop ends, give the user a summary:

- **Addressed** — each finding you fixed, and what changed.
- **Ignored** — each finding you didn't, **with the reason**.
- The cycle count and the outcome (converged, or stopped at the cap with N open).
