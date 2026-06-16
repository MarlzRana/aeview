---
name: aeview-commits
description: Review one or more specific commits with the aeview reviewer panel. Presents the review and stops; does not change code unless you explicitly ask.
argument-hint: '[<sha>[,<sha>…]] [--reviewers a,b] [aeview run flags]'
disable-model-invocation: true
---

# aeview-commits

Review specific commits with the `aeview` panel. **Review-only — do not change code unless the user
explicitly asks for a fix.**

Raw arguments: `$ARGUMENTS`

## 1. Resolve the commits

- **Commits**: the first bare token — one SHA, or a comma-separated list (`a1b2c3,d4e5f6`). If none
  is given, default to `HEAD` (the latest commit).
- Everything else (`--reviewers a,b`, `--include-dirty`, …) forwards to `aeview run`.

## 2. Run the review (prefer the background)

Always pass `--json` (the JSON gate is the reliable contract). A full panel takes a few minutes, so
**run it as a background task** rather than blocking:

```bash
aeview run --scope commits:<sha>[,<sha>…] --json [--reviewers …] [flags]
```

It prints its run id on stderr; let it finish (`aeview status <run-id> --wait`), then read the JSON
gate and present the review faithfully — verdict + each finding (severity, `file:line`, title,
recommendation). Do not editorialize or propose fixes. The exit code is the verdict: `0` approve ·
`1` needs-attention · `2` error; full report via `aeview result <run-id>`.

## 3. Stop

After presenting the review, **STOP**. Do not fix anything on your own initiative — apply fixes only
if the user explicitly asks, or point them to the `aeview-loop` skill.
