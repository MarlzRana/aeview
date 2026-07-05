---
name: aeview-loop-with-confirmation
description: Like aeview-loop — run after you and the user have planned and implemented a change — but it pauses every cycle to confirm each reviewer finding with the user before acting. For each finding it presents the issue, tailored resolution options (always including Ignore) and its recommendation via AskUserQuestion, then applies the chosen option. Use when you want to approve every fix the loop makes; for the autonomous version use aeview-loop.
argument-hint: '[--reviewers a,b] [--no-commit] [--max-cycles n] [--post-comments] [what to build or fix]'
disable-model-invocation: true
---

# aeview-loop-with-confirmation

Like the `aeview-loop` skill — run after you and the user have planned and implemented a change,
then loop with the `aeview` reviewer panel until it converges — **but the user confirms every
finding before anything is fixed or ignored**. You run the gates, run the panel, and apply the
chosen resolutions — the change is already written before you start, so you fix what the panel finds
rather than build from scratch. You do **not** triage on your own: you recommend, the user decides.

Raw arguments: `$ARGUMENTS`

- Freeform text is context for the review — the scope/files to focus on, or a note on what the
  change does.
- `--reviewers a,b` and other `aeview run` flags pass straight through to the panel.
- `--no-commit` — don't commit between cycles; work stays in the tree (changes the review scope).
- `--max-cycles <n>` — override the default cap of 5 review cycles (the loop stops as soon as it
  converges regardless). The user can also name this in plain language ("at most 3 cycles").
- `--post-comments` — post each cycle's review onto the PR and confirm findings **through the PR
  threads** (the user replies there; AskUserQuestion isn't used). Forces `--scope pr`, needs an open
  PR (creates one if missing), and rejects `--no-commit`.

## Run the loop

Follow **one** loop, matching the flags — the two are complete and self-contained, so follow only
the one that applies:

- **Default** → [references/loop.md](references/loop.md) (confirm each finding via AskUserQuestion).
- **With `--post-comments`** → [references/loop-post-comments.md](references/loop-post-comments.md)
  (post the review to the PR; the user confirms by replying in the threads).

## Required summary

When the loop ends, give the user a summary:

- **Addressed** — each finding the user chose to fix, and what changed.
- **Ignored** — each finding the user chose to ignore, with their reason.
- The cycle count and the outcome (converged, stopped at the cap with N open, or stopped on request).
