---
name: aeview-effective-pr
description: Review the branch's commits plus uncommitted work (the effective PR) with the aeview reviewer panel. Presents the review and stops; does not change code unless you explicitly ask.
argument-hint: '[--base <ref>] [--reviewers a,b] [aeview run flags]'
disable-model-invocation: true
---

# aeview-effective-pr

Review the **effective PR** — the branch's commits **plus** any uncommitted work, against the
base — with the `aeview` panel. This is what your branch would look like as a PR right now.
**Review-only — do not change code unless the user explicitly asks for a fix.**

Raw arguments: `$ARGUMENTS`

## 1. Resolve the base

- **`--base <ref>`**: compare against `<ref>` → `effective-pr:<ref>`. If omitted, use bare
  `effective-pr` (the base is auto-detected from the branch's open PR, else `origin/HEAD` /
  `main`/`master`/`trunk`).
- Everything else (`--reviewers a,b`, `--allow-conflicts`, …) forwards to `aeview run`.

## 2. Run the review (prefer the background)

Always pass `--json` (the JSON gate is the reliable contract). A full panel takes a few minutes, so
**run it as a background task** rather than blocking:

```bash
aeview run --scope effective-pr[:<base>] --json [--reviewers …] [flags]
```

It prints its run id on stderr; let it finish (`aeview status <run-id> --wait`), then read the JSON
gate and present the review faithfully — verdict + each finding (severity, `file:line`, title,
recommendation). Do not editorialize or propose fixes. The exit code is the verdict: `0` approve ·
`1` needs-attention · `2` error; full report via `aeview result <run-id>`.

## 3. Stop

After presenting the review, **STOP**. Do not fix anything on your own initiative — apply fixes only
if the user explicitly asks, or point them to the `aeview-loop` skill.
