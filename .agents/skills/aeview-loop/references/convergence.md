# The convergence loop (when to keep iterating, when to stop)

The rule for when an implement-and-review loop is **done**. You are the implementer: you write the
code, run the gates, run the panel, triage, and fix.

## One-liner

The loop has **converged** when a fresh cycle surfaces **no new *actionable* findings** — only
things already triaged away — and the trend has flattened. Convergence is **not** "zero findings."

## The loop

1. **Implement / fix.** Cycle 1: make the change. Later cycles: fix the actionable findings from the
   previous panel.
2. **Run the project's hard gates** (tests, lint, type-check — see *Gates* below). These must pass
   before you go on; fix any failures first.
3. **Commit** the cycle's work with a conventional message (skip if the user asked for `--no-commit`).
4. **Run the aeview panel** over the change and read the findings.
5. **Triage** every finding (see *Implementer judgment*); fix what's worth fixing.
6. **Re-run from step 1** against the updated change.

## A cycle is converged when everything it surfaces is one of:

- a finding the user has **deferred** to later (and you logged as such), or
- a **by-design tradeoff explicitly accepted** by the user (won't-fix), or
- **dismissable noise** — a false positive, or a preference nit where the code already follows the
  project's conventions.

If a cycle returns findings but **none are new + actionable**, it has converged.

## Not converged

A cycle still finds fresh, real, actionable problems — bugs, missing-test gaps, ownership-boundary
violations, security/contract issues — **including bugs introduced by the previous cycle's own
fixes**. Keep going: fix → re-review.

## Implementer judgment (the triage is yours)

**You hold the call** on whether to **address or ignore** each finding — weighing severity,
correctness, real-bug-vs-preference, ownership boundary, whether the code already follows the
project's conventions, and whether it's a known deferral.

The one exception: if a finding implies a **key design decision**, or one where the user's
**approval would be useful**, do **not** decide alone — flag it and ask.

### The reviewers see only the diff — you hold the context

The panel judges the change **in isolation**. It does not know the project context in this session
and the maintainer's head: who the users are, released vs unreleased, decisions already made ("we
don't support X", "single user — no migration needed"), or what's deferred. So a finding can be
*technically correct about the code* yet premised on a scenario that cannot occur — e.g. "existing
users upgrading would break" when there are no other users, or "add a migration for the old format"
when migration was explicitly declined.

Filter every finding against this hidden context and **ignore (with reason)** those premised on a
non-existent scenario — even high-confidence, high-agreement ones (agreement just means the
reviewers share the same blind spot). Building code to satisfy such a finding is over-engineering.
Prefer the leaner code and record the reason. When genuinely unsure whether the context rules it
out, flag it rather than guess.

## Gates (the project's own, always required)

The panel is a strong empirical signal, not a correctness proof — so each cycle also runs the
project's **hard gates** and they must pass regardless of the panel's state. Discover them rather
than assuming a stack:

- **Config / manifest** — `pyproject.toml` / `tox.ini` (pytest, ruff, mypy/pyright), `package.json`
  scripts (`test`, `lint`, `typecheck`), `Cargo.toml`, `go.mod`, `Makefile` / `justfile` targets.
- **CI** — `.github/workflows/*`, `.gitlab-ci.yml`, etc. show the canonical commands the project
  trusts; mirror those.
- If none are discoverable or they're ambiguous, **ask the user** which commands count as the gates.

## Hard bounds

- **Minimum 2 cycles** — even if cycle 1 looks clean. Fixes can regress, so every fix round gets
  re-reviewed at least once.
- **Maximum 5 cycles** — a hard cap. If it hasn't converged by cycle 5, **stop anyway** and report
  the remaining open findings rather than looping further. (Not converging in 5 cycles is itself
  worth surfacing.)

## Required summary after the last cycle

After the final cycle (convergence or the cap), give a summary listing:

- **Addressed** — each finding you fixed, with what changed.
- **Ignored** — each finding you did *not* address, **with the reason** (deferred / accepted
  tradeoff / false positive / preference-only / reviewer lacks context).
- The cycle count and the outcome (converged, or stopped at the cap with N open).
