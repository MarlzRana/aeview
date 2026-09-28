---
name: aeview-update
description: Update the aeview CLI and its reviewer-panel skills on this machine to the latest published versions, then verify.
argument-hint: '[--pipx]'
disable-model-invocation: true
---

# aeview-update

Bring `aeview` up to date on this machine: upgrade the CLI to the latest PyPI release, refresh the
reviewer-panel skills, and verify. Run this whenever a new version has shipped.

Raw arguments: `$ARGUMENTS` — `--pipx` forces the pipx path instead of auto-detecting.

## 1. Note the current version

```bash
aeview --version
```

Remember it so you can confirm the upgrade landed.

## 2. Upgrade the CLI

Upgrade aeview with whichever tool installed it. Prefer uv unless `--pipx` was passed.

Default (uv — the CLI shows up in `uv tool list`):

```bash
uv tool upgrade aeview
```

Or with `--pipx` (or if aeview was installed with pipx):

```bash
pipx upgrade aeview
```

`uv tool upgrade` / `pipx upgrade` resolve the latest release from PyPI themselves — no version pin
needed.

## 3. Update the skills globally

Re-pull the aeview skills so any prompt changes land too — the same set the installer uses, with no
prompts:

```bash
npx skills add MarlzRana/aeview -g -y \
  --skill aeview aeview-install aeview-update aeview-pr aeview-loop aeview-loop-with-confirmation aeview-commits aeview-effective-pr
```

`-g` installs for every project (the global agent dir) and `-y` skips the prompts. The skills are
listed by name — not `--skill '*'` — so only the `aeview*` skills update: the repo also carries a
maintainer-only `release` skill that end users shouldn't get.

## 4. Verify

```bash
aeview --version
aeview doctor
```

Confirm the version advanced from step 1. `aeview doctor` reports what's missing for the reviewers you
have — most importantly **harness auth** (aeview drives Claude Code / Codex / Copilot through bundled
SDKs, but you must be authenticated with each harness a reviewer uses). Resolve anything it flags.

Note: aeview seeds `~/.aeview` defaults **write-if-absent** and never overwrites your edits, so an
upgrade won't refresh already-seeded config (`settings.json`, `DEDUPLICATION.md`, the `default`
reviewer). If a release changes a default and you want it, move your customized file aside and re-run
so the new version seeds.
