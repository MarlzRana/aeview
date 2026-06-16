#!/usr/bin/env bash
# Release preflight for a uv + trusted-publishing project.
#
# Reports release context (branch / sync / version / recent tags) and then BUILDS and validates the
# distribution artifacts (`uv build` + `uvx twine check --strict`) — the tedious must-pass checks —
# before anything irreversible (the tag push). Run from the repo you're releasing.
#
# Read-only except for writing the gitignored dist/. Exits non-zero if the build or twine check
# fails (i.e. NOT ready to release); informational checks only warn.
set -uo pipefail

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "ERROR: not inside a git repo"; exit 1; }
cd "$repo_root" || exit 1

echo "== context =="
echo "repo:     $repo_root"
echo "branch:   $(git rev-parse --abbrev-ref HEAD)"
if git fetch --quiet origin 2>/dev/null && git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
  echo "upstream: ahead $(git rev-list --count '@{u}..HEAD'), behind $(git rev-list --count 'HEAD..@{u}')"
else
  echo "upstream: (no tracking branch, or fetch failed)"
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "tree:     DIRTY — commit/stash before tagging (the tag must point at a pushed commit)"
else
  echo "tree:     clean"
fi
echo "version:  $(uv version --short 2>/dev/null || echo '? (uv version failed — is this a uv project?)')"
echo "tags:     $(git tag --list 'v*' | sort -V | tail -3 | tr '\n' ' ')"

echo
echo "== build + validate artifacts (must pass before publishing) =="
rm -rf dist
uv build || { echo "BUILD FAILED — not ready to release."; exit 1; }
uvx twine check --strict dist/* || { echo "TWINE CHECK FAILED — not ready to release."; exit 1; }

echo
echo "PREFLIGHT OK — artifacts build and pass twine check."
echo "Next: bump pyproject version -> commit -> push branch -> tag v<version> + push (the trigger)"
echo "      -> approve the 'pypi' deployment gate -> verify the publish."
