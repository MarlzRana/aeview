---
name: aeview-pr
description: Review a GitHub pull request with the aeview reviewer panel. Reviews the PR's diff and (by default) checks the PR out so reviewers read its real files. Presents the review and stops; does not change code unless you explicitly ask.
argument-hint: '[<pr-number>] [--checkout new|current|none] [--reviewers a,b] [aeview run flags]'
disable-model-invocation: true
---

# aeview-pr

Review a pull request with the `aeview` panel. **Review-only — do not change code unless the user explicitly asks for a fix.**

Raw arguments: `$ARGUMENTS`

## 1. Resolve the PR + flags

- **PR number**: the first bare number in the arguments. If none, the current branch's PR:
  `gh pr view --json number -q .number`.
- **`--checkout new|current|none`**: how to make the PR's files available to reviewers (step 2). If
  omitted, ask.
- Everything else (`--reviewers a,b`, `--include-dirty`, …) forwards to `aeview run`.

## 2. Check out the PR

`aeview run --scope pr:<n>` gets the diff via `gh`, but reviewers read the working tree — for changed
files that's the wrong version unless the PR is checked out. Read [the PR-checkout reference](references/pr-checkout.md) and
follow it: honor `--checkout`, else offer new-worktree (recommended) / current / none.

## 3. Run the review (prefer the background)

From the chosen location (the PR worktree, the current repo, or wherever you are for no-checkout).
Always pass `--json` (these skills are agent-driven; the JSON gate is the reliable contract). A full
panel takes a few minutes, so **run it as a background task** rather than blocking:

```bash
aeview run --scope pr:<n> --json [--reviewers …] [flags]
```

It prints its run id on stderr; let it finish (`aeview status <run-id> --wait`), then read the JSON
gate and present the review faithfully — verdict + each finding (severity, `file:line`, title,
recommendation). Do not editorialize or propose fixes. The exit code is the verdict: `0` approve ·
`1` needs-attention · `2` error; full report via `aeview result <run-id>`.

## 4. Stop

After presenting the review, **STOP**. Do not fix anything on your own initiative — apply fixes only
if the user explicitly asks, or point them to the `aeview-loop` skill.
