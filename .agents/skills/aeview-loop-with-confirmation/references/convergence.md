# The convergence loop, with confirmation (when to keep iterating, when to stop)

The rule for when an implement-and-review loop is **done** — the variant where **the user confirms
every finding**. You are the implementer: you write the code, run the gates, run the panel, and
apply the resolutions the user picks. You do **not** triage autonomously — you recommend, the user
decides.

## One-liner

The loop has **converged** when a fresh cycle surfaces **no new findings the user elects to fix** —
only findings already decided in an earlier cycle. Convergence is **not** "zero findings."

## The loop

1. **Implement / fix.** Cycle 1: make the change. Later cycles: the user's chosen fixes from the
   previous cycle were already applied at the end of step 5 — this cycle re-gates and re-reviews them.
2. **Run the project's hard gates** (tests, lint, type-check — see *Gates* below). These must pass
   before you go on; fix any failures first.
3. **Commit** the cycle's work with a conventional message (skip if the user asked for `--no-commit`).
4. **Run the aeview panel** over the change and read the findings.
5. **Confirm every finding with the user** (see *Confirming findings*); apply each choice.
6. **Re-run from step 1** with the chosen fixes in place.

## Confirming findings (the decision is the user's)

Each cycle, present **every** finding to the user — do not silently drop or auto-fix any. Use
AskUserQuestion, batching up to 4 findings per call, each with:

- the finding stated plainly — severity, `file:line`, title, and the reviewer's recommendation, and
- **1–3 tailored resolution options plus an always-present "Ignore"**, with your recommended option
  first and marked "(recommended)".

You **recommend**, the user **decides**. Apply a fix choice immediately; record an Ignore (with the
user's reason) for the summary.

### Surface the context — don't pre-filter on it

The panel judges the change **in isolation**: it can't see the project context that lives in this
session and the maintainer's head — who the users are, released vs unreleased, decisions already
made ("we don't support X", "single user — no migration needed"), or what's deferred. So a finding
can be *technically correct about the code* yet premised on a scenario that cannot occur — e.g.
"existing users upgrading would break" when there are no other users, or "add a migration for the
old format" when migration was explicitly declined.

In the autonomous `aeview-loop` you would quietly ignore those. **Here you do not.** Surface every
such finding, but make your recommendation **"Ignore (recommended)"** and say why in the option's
description ("reviewers don't know there are no other users / migration was declined / this is
unreleased"). The user makes the call — they may know something you don't, or agree and dismiss it
in one click. When you're genuinely unsure whether the context rules a finding out, say so in the
question rather than steering.

### Don't re-ask decided findings

Track the findings the user has already decided on. Each later cycle, only put **new** findings to
the user; carry earlier Ignore decisions forward (still listed in the summary) and don't re-ask
about them — otherwise the loop can never converge. A reappearing, already-ignored finding is not a
reason to keep looping; a genuinely *new* finding (including a bug introduced by the previous cycle's
own fixes) is.

## Gates (the project's own, always required)

The panel is a strong empirical signal, not a correctness proof — so each cycle also runs the
project's **hard gates** and they must pass regardless of the panel's state. Discover them rather
than assuming a stack:

- **Config / manifest** — `pyproject.toml` / `tox.ini` (pytest, ruff, mypy/pyright), `package.json`
  scripts (`test`, `lint`, `typecheck`), `Cargo.toml`, `go.mod`, `Makefile` / `justfile` targets.
- **CI** — `.github/workflows/*`, `.gitlab-ci.yml`, etc. show the canonical commands the project
  trusts; mirror those.
- If none are discoverable or they're ambiguous, **ask the user** which commands count as the gates.

## Cycle bounds

- **Minimum** — run at least this many cycles even if cycle 1 looks clean; fixes can regress, so
  every fix round gets re-reviewed at least once. **Default 2.**
- **Maximum** — a cap so the loop can't run away; if it hasn't converged by then, stop and report
  the open findings. **Default 5.**
- **The user sets these.** If the user specifies a minimum and/or maximum — via `--min-cycles <n>` /
  `--max-cycles <n>` or in their request — use those values instead of the defaults.

## Required summary after the last cycle

After the final cycle (convergence, the cap, or a stop request), give a summary listing:

- **Addressed** — each finding the user chose to fix, with what changed.
- **Ignored** — each finding the user chose to ignore, with their reason.
- The cycle count and the outcome (converged, stopped at the cap with N open, or stopped on request).
