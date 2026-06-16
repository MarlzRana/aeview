---
name: aeview
description: Review code changes with the aeview multi-harness reviewer panel. Use when the user asks to review a diff, branch, pull request, commits, or staged/working changes, wants a code review, or wants changes checked before merge or commit. You pick the scope from the request and may override reviewers and settings. Presents the review and stops; does not change code unless the user explicitly asks. For an implement-and-review loop, use the aeview-loop skill instead.
argument-hint: '[--scope <type[:value]>] [--reviewers a,b] [aeview run flags] [what to review]'
---

# aeview

Run the `aeview` reviewer panel over a scope and present the review. **Review-only — do not change code unless the user explicitly asks for a fix.**

Raw arguments (may be empty): `$ARGUMENTS`

## 1. Resolve the scope

Pick exactly one `aeview` scope from the request and the raw arguments. If the user gave explicit
`--scope`/`--reviewers`/other flags, forward them verbatim. Otherwise map their intent:

| The user wants to review… | scope |
|---|---|
| their uncommitted / current changes, or it's unspecified | `auto` (just omit `--scope`) |
| only the staged changes | `staged` |
| a branch against its base | `branch[:<base>]` |
| one or more specific commits | `commits:<sha>[,<sha>…]` |
| a pull request | **handle checkout first — see step 2** |
| the branch's commits **plus** uncommitted work | `effective-pr[:<base>]` |
| a diff/patch file (or stdin) | `patch:<file>` (or `patch:-`) |

Reviewer/setting overrides (`--reviewers a,b`, `--include-dirty`, `--allow-conflicts`, …) pass
straight through to `aeview run`.

## 2. Pull request scope — checkout first (only if reviewing a PR)

If the user is reviewing a PR, handle checkout **before** running. It's **optional**: `--scope
pr:<n>` gets the diff via `gh`, but the reviewers read the working tree, so checking the PR out lets
them read its real files. Read [the PR-checkout reference](references/pr-checkout.md) and follow it — it offers new-worktree
(recommended) / current / **none** (diff-only, if the user doesn't want a checkout). Then run the
review (step 3) from the chosen location with `--scope pr:<n>`.

## 3. Run the review (prefer the background)

Always pass `--json` — these skills are agent-driven, and the JSON gate is the reliable contract to
consume. A full panel takes a few minutes, so **run it as a background task** rather than blocking:

```bash
aeview run --scope <resolved> --json [--reviewers …] [other flags]
```

(For a PR, run from the checkout location chosen in step 2.) It prints its run id on stderr; let it
finish (`aeview status <run-id> --wait`), then read the JSON gate and present the review faithfully
— verdict + each finding (severity, `file:line`, title, recommendation). Do not editorialize or
propose fixes. The exit code is the verdict: `0` approve · `1` needs-attention · `2` error. The full
report is `aeview result <run-id>`.

## 4. Stop

After presenting the review, **STOP**. Do not fix issues or apply patches on your own initiative.
Apply fixes only if the user explicitly asks; for an implement-and-review loop, point them to the
`aeview-loop` skill. Never auto-apply fixes from a review unprompted.
