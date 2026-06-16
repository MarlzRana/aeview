# Checking out a PR for review

`aeview run --scope pr:<n>` gets the PR diff via `gh`, but the reviewers read the **current working
tree** — so for changed files they would read the wrong version. Check the PR out first so the
reviewers see its real files.

## Choose where to check out

If a `--checkout new|current|none` argument was given, honor it. Otherwise ask with AskUserQuestion:

- **New worktree (recommended)** — isolate the PR in a worktree under `~/.aeview/worktrees/`; your
  current tree is untouched.
- **Current worktree** — check the PR out in place (only if the working tree is clean).
- **No checkout** — review the diff only; reviewers read the current tree, so changed files may be
  stale.

## New worktree (per-PR, reused, kept)

[Run the bundled worktree script](../scripts/pr-worktree.sh). It fetches the PR head, creates or
refreshes a per-PR worktree under `~/.aeview/worktrees/<repo>-pr-<n>`, and prints its path:

```bash
wt=$(../scripts/pr-worktree.sh <n>)
```

(If `origin` is not the repo hosting the PR, the `pull/<n>/head` fetch fails — fetch the PR head
from the correct remote first; `gh pr view <n> --json headRepository` shows it.)

Then run the review **from the worktree** and present its output verbatim:

```bash
( cd "$wt" && aeview run --scope "pr:<n>" --json [--reviewers …] [flags] )
```

Leave the worktree in place — it is reused on the next review of this PR.

## Current worktree

Only if `git status --porcelain` is empty. `gh pr checkout <n>`, then `aeview run --scope pr:<n> --json`
from the repo root.

## No checkout

`aeview run --scope pr:<n> --json` from wherever you are. Note in the output that the reviewers read the
current tree, so the changed files may not reflect the PR's actual contents.
