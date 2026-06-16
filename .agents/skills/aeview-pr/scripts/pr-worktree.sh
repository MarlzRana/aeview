#!/usr/bin/env bash
# Create or refresh a per-PR review worktree on the PR head, and print its path.
# Worktree: ~/.aeview/worktrees/<repo>-pr-<n>. Reused if it already exists; kept after the review.
# Usage: pr-worktree.sh <pr-number>
set -euo pipefail

n="${1:?usage: pr-worktree.sh <pr-number>}"
repo="$(basename "$(git rev-parse --show-toplevel)")"
wt="$HOME/.aeview/worktrees/${repo}-pr-${n}"

# pull/<n>/head is the PR head on the repo that hosts the PR (origin in the common case).
git fetch --quiet origin "pull/${n}/head"

if [ -d "$wt" ]; then
  git -C "$wt" checkout --quiet --detach FETCH_HEAD   # reuse: refresh to the latest PR head
else
  mkdir -p "$(dirname "$wt")"
  git worktree add --quiet --detach "$wt" FETCH_HEAD
fi

printf '%s\n' "$wt"
