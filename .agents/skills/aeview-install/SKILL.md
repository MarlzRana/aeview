---
name: aeview-install
description: Install the aeview CLI and its reviewer-panel skills on this machine, then verify the setup.
argument-hint: '[--pipx]'
disable-model-invocation: true
---

# aeview-install

Set up `aeview` on this machine: install the CLI from PyPI, install the reviewer-panel skills
globally, and verify. A one-time setup.

Raw arguments: `$ARGUMENTS` — `--pipx` uses pipx instead of uv for the CLI.

## 1. Install the CLI

Default (uv — also fetches Python 3.14 if needed):

```bash
uv tool install --prerelease=allow aeview
```

The `--prerelease=allow` flag is required for **uv** only: aeview pulls a prerelease Codex runtime,
so uv needs it for the transitive prerelease. With `--pipx`, no flag is needed:

```bash
pipx install aeview
```

## 2. Install the skills globally

Pull the aeview skills — this one, plus `aeview`, `aeview-pr`, `aeview-loop`, `aeview-commits`, and
`aeview-effective-pr` — from the public repo and install them globally, with no prompts:

```bash
npx skills add MarlzRana/aeview -g -y \
  --skill aeview aeview-install aeview-pr aeview-loop aeview-commits aeview-effective-pr
```

`-g` installs for every project (the global agent dir) and `-y` skips the prompts. The skills are
listed by name — not `--skill '*'` — so only the `aeview*` skills install: the repo also carries a
maintainer-only `release` skill that end users shouldn't get, and the `skills` CLI matches `--skill`
names exactly (there's no `aeview*` glob). It installs to whichever agents the CLI detects on this
machine.

## 3. Verify

```bash
aeview --version
aeview doctor
```

`aeview doctor` reports what's missing for the reviewers you have — most importantly **harness auth**
(aeview drives Claude Code / Codex / Copilot through bundled SDKs, but you must be authenticated with
each harness a reviewer uses). Resolve anything it flags. aeview requires **Python 3.14+** on
**macOS or Linux**.
