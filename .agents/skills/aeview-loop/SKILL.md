---
name: aeview-loop
description: Implement or fix code and then iterate with the aeview reviewer panel until it converges. Use when the user wants you to build, change, or fix something and keep going until it passes the project's gates and the panel finds no new issues — an implement-review-fix loop, "review until clean", or "loop until the panel is happy". For a one-shot review with no changes use the aeview skill instead; for a plain edit with no review loop, just edit.
argument-hint: '[--reviewers a,b] [--no-commit] [what to build or fix]'
---

# aeview-loop

Implement a change, then loop with the `aeview` reviewer panel until it converges. **You are the
implementer** — you write the code, run the gates, run the panel, triage the findings, and fix.
Unlike the `aeview` skill (review-only), this skill *does* change code: that's the whole point.

Raw arguments: `$ARGUMENTS`

- Treat the freeform text as the task to implement (and, if given, the scope/files to focus on).
- `--reviewers a,b` and other `aeview run` flags pass straight through to the panel.
- `--no-commit` — don't commit between cycles; work stays in the tree (changes the review scope, see
  step 4).

## The loop (min 2 cycles, max 5)

Read [the convergence reference](references/convergence.md) once — it defines convergence, the
triage rules, the gate-discovery guidance, and the bounds. Then run the loop:

### 1. Implement / fix

Cycle 1: make the change the user asked for. Later cycles: fix the actionable findings from the
previous panel.

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

Stop when a cycle surfaces **no new actionable findings** (not necessarily zero) — after at least 2
cycles — or when you hit cycle 5, whichever comes first.

## Required summary

When the loop ends, give the user a summary:

- **Addressed** — each finding you fixed, and what changed.
- **Ignored** — each finding you didn't, **with the reason**.
- The cycle count and the outcome (converged, or stopped at the cap with N open).
