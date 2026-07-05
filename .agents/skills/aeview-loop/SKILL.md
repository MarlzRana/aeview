---
name: aeview-loop
description: After you and the user have planned and implemented a change, loop with the aeview reviewer panel until it converges — running the project's gates, reviewing with the panel, and fixing the findings each cycle. Use when a change is already written and the user wants it reviewed and fixed until it passes the gates and the panel finds no new issues — "review until clean" or "loop until the panel is happy". It fixes what the panel finds; it does not build the change from scratch. For a one-shot review with no fixes, use the aeview skill instead.
argument-hint: '[--reviewers a,b] [--no-commit] [--max-cycles n] [--post-comments] [what to build or fix]'
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
- `--post-comments` — post each cycle's review onto the PR and reply on every finding's thread as an
  audit trail. Forces `--scope pr`, needs an open PR (creates one if missing), and rejects
  `--no-commit`.

## Run the loop

First read [the convergence reference](references/convergence.md) — it defines convergence, the
triage rules, the gate-discovery guidance, and the bounds. Then follow **one** loop, matching the
flags — the two are complete and self-contained, so follow only the one that applies:

- **Default** → [references/loop.md](references/loop.md).
- **With `--post-comments`** → [references/loop-post-comments.md](references/loop-post-comments.md)
  (posts the review to the PR and works the findings as PR threads).

## Required summary

When the loop ends, give the user a summary:

- **Addressed** — each finding you fixed, and what changed.
- **Ignored** — each finding you didn't, **with the reason**.
- The cycle count and the outcome (converged, or stopped at the cap with N open).
