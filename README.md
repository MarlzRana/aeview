# aeview

**Fan code reviewers across multiple AI agent harnesses, then merge one deduplicated verdict.**

aeview runs the *same* change past several reviewers — each a prompt you check into your repo —
across several agent harnesses (Claude Code, Codex, Copilot), in parallel. It collects every
finding, deduplicates them with an LLM judge, and writes a single `report.json` plus an exit code
you can loop on: `0` approve · `1` needs-attention · `2` error.

The bet: a *reviewer* is a versioned artifact (a prompt + the harnesses it runs on), and a panel of
independent models catches what any single model misses.

```sh
uv tool install aeview
cd your-repo
aeview run          # review your current changes (auto mode); exit 1 if anything needs attention
```

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Quickstart](#quickstart)
- [Scopes — what to review](#scopes--what-to-review)
- [Reviewers](#reviewers)
- [Auto mode, activation & `.aeviewignore`](#auto-mode-activation--aeviewignore)
- [Harnesses](#harnesses)
- [Configuration](#configuration)
- [Commands](#commands)
- [The report & exit codes](#the-report--exit-codes)
- [Runs, lifecycle & output](#runs-lifecycle--output)
- [Development](#development)
- [License](#license)

---

## How it works

A **reviewer** is a prompt (`REVIEWER.md`) plus a set of **harness instances** (an agent + a model).
One `aeview run` does this:

1. **Resolve reviewers** — walk up from your cwd to home collecting `.aeview/reviewers/*`.
2. **Resolve the scope** — turn `--scope` into a concrete git diff (working tree, a branch, a PR, …).
3. **Bundle the diff** — small diffs are frozen inline; large ones hand the agent read-only git to
   self-collect, so nothing is silently truncated.
4. **Fan out** — run every `reviewer × harness` pair concurrently, each in a **read-only** sandbox
   (read anywhere, write nothing). Each emits findings against a fixed schema.
5. **Dedupe & merge** — an LLM judge groups duplicate findings across the panel; survivors are kept
   verbatim with their provenance and an agreement count.
6. **Report** — write `report.json` and exit `0` / `1` / `2`.

Everything is persisted under `~/.aeview/runs/<id>/`, so a killed run can be `resume`d.

## Requirements

- **Python 3.14+** (the `uv` installer can fetch one for you).
- **macOS or Linux.**
- **Harness auth.** aeview drives each harness through its Python SDK, which **bundles a pinned CLI
  binary** — you don't install Claude Code / Codex / Copilot separately. You *do* need to be
  authenticated with each harness a reviewer uses. Run [`aeview doctor`](#commands) to see exactly
  what's missing for the reviewers you have.
- **`gh`** (GitHub CLI) — for `--scope pr`, and to auto-detect a branch's base from its open PR.
  Optional: aeview falls back to `origin/HEAD`, then `main`/`master`/`trunk`, when `gh` or a PR
  isn't available.

## Install

```sh
uv tool install aeview      # recommended
# or
pipx install aeview
```

> **Note — no special flags needed.** aeview currently depends on a *prerelease* Codex runtime
> (the Codex Python SDK is still in beta), so you'll see `openai-codex` / `openai-codex-cli-bin`
> alpha/beta versions in your install. Both `uv` and `pip` resolve these automatically because the
> prerelease markers are baked into aeview's own dependencies — you do **not** need
> `--prerelease=allow` or `--pre`. (Homebrew support is planned.)

After install, sanity-check your setup:

```sh
aeview doctor
```

## Quickstart

```sh
cd your-repo

# Review your changes (auto mode: uncommitted work if the tree is dirty, else your branch vs base):
aeview run

# Preview the plan (roster + scope + bundle size) without spending anything:
aeview run --dry-run

# Review a branch against its base, with every reviewer you have:
aeview run --scope branch --reviewers all

# Get machine-readable output and act on the exit code:
aeview run --json && echo "approved" || echo "needs attention (or error)"
```

A run prints a human summary (or `--json`), writes `report.json`, and exits with the verdict code.

## Scopes — what to review

`--scope <type>[:value]`. Omit `--scope` entirely for **`auto`**. Where a type takes a value, the
"bare" form (no `:value`) uses the default shown:

| Scope | Bare default | Reviews |
|---|---|---|
| `working-tree` | — | all uncommitted changes |
| `staged` | — | only the staged changes |
| `branch[:base]` | base = auto | your branch vs `base` |
| `pr[:number]` | current branch's PR | a PR's diff (via `gh`) |
| `effective-pr[:base]` | base = auto | branch commits **+** uncommitted work, vs `base` |
| `commits[:a,b,c]` | `HEAD` | exactly the commits listed, each vs its own parent |
| `range:A..B` | *value required* | the diff between `A` and `B` |
| `patch:file` | *value required* | a diff file (or `-` for stdin) |
| `auto` | — | dirty → `working-tree`, else your branch vs base |

Notes:

- **`commits`** is a *set*, not a range: `--scope commits:a,c,e` reviews exactly those three commits,
  each shown against its own parent (a union of per-commit patches). A single commit is just the
  one-element case; bare `commits` is `HEAD`.
- **`--include-dirty`** folds uncommitted work onto a committed scope — use it with `branch`,
  `auto`, or `staged`; it's a no-op on `working-tree`.
- **`--allow-conflicts`** reviews despite an in-progress merge/rebase (refused by default).
- Base resolution order: explicit value → the PR base → `origin/HEAD` → `main`/`master`/`trunk`.

## Reviewers

A reviewer lives at `.aeview/reviewers/<name>/REVIEWER.md`: YAML frontmatter + a prompt body.

```markdown
---
name: security
description: Hunts auth, injection, and trust-boundary bugs.
harnesses:
  - { harness: claude-code, model: claude-opus-4-8 }
  - { harness: codex, model: gpt-5.5, thinking: xhigh }
auto-activate-paths:
  - "src/api/**"
---

You are a security reviewer. Find the injection, the auth gap, the unsafe
deserialization... (the rest of the prompt)
```

Frontmatter fields:

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Must equal the directory name. |
| `description` | no | One line, shown by `aeview reviewers`. |
| `harnesses` | no | List of `{ harness, model, thinking? }`. **Omit** to use the global `fallbackReviewerHarnesses`. |
| `auto-activate-paths` | no | Globs that opt this reviewer into [auto mode](#auto-mode-activation--aeviewignore). |

### Resolution (walk-up)

aeview climbs from your cwd through its parent directories up to your home directory, looking for
`<dir>/.aeview/reviewers/<name>/REVIEWER.md`. **First match wins**, so a repo-local reviewer shadows
a personal one of the same name. Your home `~/.aeview/reviewers/` holds reviewers available
everywhere — including the seeded **`default`** reviewer (an adversarial general-purpose reviewer).

### Selecting reviewers

```sh
aeview run --reviewers security              # one
aeview run --reviewers security,tests        # several (comma-separated)
aeview run --reviewers security --reviewers tests   # or repeated
aeview run --reviewers all                   # every reviewer visible here
```

With no `--reviewers`, aeview uses [auto mode](#auto-mode-activation--aeviewignore).

### Resource references

The reviewer's own directory (absolute path) is prepended to its prompt, so you can drop supporting
files beside `REVIEWER.md` (`references/checklist.md`, scripts, …) and reference them with relative
paths. Reviewers can read anywhere, so the links resolve.

### Scaffolding

```sh
aeview init security                 # create .aeview/reviewers/security/REVIEWER.md
aeview init security --with-harness  # also scaffold a harnesses: block
```

## Auto mode, activation & `.aeviewignore`

**Auto mode** (no `--reviewers`) builds the roster as:

> **`default`** (always) **∪** every reviewer whose `auto-activate-paths` matches a changed file.

A reviewer with no `auto-activate-paths` never auto-runs (select it by name or with `all`). Matching
uses **literal globs** (Python's `PurePath.full_match`), anchored at the reviewer's `.aeview` parent
directory: a single `*` stops at `/`, `**` crosses directories, there's no negation, and it's
case-sensitive. So write `backend/**`, not `backend/`.

Auto-activation needs repo-root-relative paths, so `--scope patch` (and running outside a git repo)
runs only `default` — name any extra reviewers explicitly there.

**`.aeviewignore`** filters files **out of the diff before it's reviewed** — handy for lockfiles,
generated code, vendored trees. It uses faithful gitignore semantics (the `pathspec` library):

```gitignore
uv.lock
dist/
**/__snapshots__/
!important.generated.ts
```

Files are collected on the same cwd→home walk (your home file is `~/.aeviewignore`); each file's
patterns anchor at its own directory, nearest rules win, and `!` negation is supported.

Like auto-activation, `.aeviewignore` is skipped for `--scope patch` (its paths aren't
repo-root-relative) — a patch is reviewed exactly as supplied.

## Harnesses

A harness instance is `{ harness, model, thinking? }`. Supported harnesses:

| `harness` | Driven via | Example `model` |
|---|---|---|
| `claude-code` | `claude-agent-sdk` | `claude-opus-4-8` |
| `codex` | `openai-codex` | `gpt-5.5` (e.g. `thinking: xhigh`) |
| `copilot` | `github-copilot-sdk` | a Copilot-served model |

Every harness runs **read-anywhere, write-nowhere**: a reviewer can read any file (to gather
context) but cannot modify your repo. Claude Code and Codex enforce this with their native
read-only sandboxes; Copilot uses a deny-by-default permission handler that only approves reads.

Each SDK ships a **pinned binary**, so aeview is insulated from your own CLI upgrades. To point a
harness at a different binary, set [`overrideHarnessBinaries`](#configuration).

## Configuration

Global settings live in `~/.aeview/settings.json`, seeded (write-if-absent) on first run. Its keys
are camelCase; the JSON run artifacts (`report.json`, `review.json`) use snake_case.

```json
{
  "fallbackReviewerHarnesses": [
    { "harness": "claude-code", "model": "claude-opus-4-8" }
  ],
  "deduplicationHarness": { "harness": "claude-code", "model": "claude-opus-4-8" },
  "retention": { "keepLast": 20, "ttlDays": 14 },
  "reviewTimeoutSeconds": 1200,
  "overrideHarnessBinaries": { "codex": "/usr/local/bin/codex" }
}
```

| Key | Meaning |
|---|---|
| `fallbackReviewerHarnesses` | Harnesses a reviewer runs on when its `REVIEWER.md` has no `harnesses:` block. |
| `deduplicationHarness` | The harness used to deduplicate findings across the panel. |
| `retention` | Auto-prune old runs: keep at least the newest `keepLast`, and drop runs that are *also* older than `ttlDays` days. |
| `reviewTimeoutSeconds` | Timeout (seconds) per harness attempt; transient errors retry, so a review's total time can exceed it. A timed-out review fails fast (no retry); `resume` re-runs it. |
| `overrideHarnessBinaries` | Optional per-harness override of the bundled CLI binary, by path. Keys: `claude-code`, `codex`, `copilot`. |

## Commands

| Command | What it does |
|---|---|
| `aeview run` | Run reviewers over a scope; print the **gate** (verdict + findings, see below) and exit `0/1/2`. |
| `aeview status [run-id]` | Per-review progress + coverage (defaults to the latest run). `--wait` blocks to a terminal state and adopts its exit code. |
| `aeview result [run-id]` | Print a finished run's **full** report; exit with its `0/1/2` code. |
| `aeview resume <run-id>` | Re-run a run's non-`done` reviews against its frozen bundle, then re-merge. |
| `aeview list` | Recent runs, newest first: id, time, scope, verdict, coverage. |
| `aeview reviewers [name]` | List the reviewers visible here (walk-up), or show one's detail. |
| `aeview init <name>` | Scaffold a repo reviewer. `--with-harness` adds a `harnesses:` block. |
| `aeview doctor` | Preflight: reviewer config, harness binaries + auth, and `gh`. Exits 1 on failure. |
| `aeview version` | Print the version. |

Common `run` flags: `--scope`, `--reviewers`, `--include-dirty`, `--allow-conflicts`, `--dry-run`,
`--json`. Most commands accept `--json` for machine-readable output.

## The report & exit codes

The full merged artifact is `report.json` — what `aeview result` and the on-disk file give you:

```jsonc
{
  "verdict": "needs-attention",          // "approve" | "needs-attention"
  "summary": "...",
  "findings": [
    {
      "id": "f3",                          // stable run-local id assigned during merge
      "title": "Unvalidated path used in file read",
      "body": "...",
      "severity": "high",                // critical | high | medium | low
      "category": "security",            // bug | security | regression | test_gap | maintainability
      "confidence": 0.9,                 // 0.0–1.0
      "location": { "file": "src/api/files.py", "line_start": 42, "line_end": 48 },
      "recommendation": "...",
      "agreement": 2,                    // size of the dedup group (findings merged into this one)
      "sources": [                         // one entry per merged finding, with its originating review
        { "review": "security__codex-gpt-5.5", "severity": "high", "confidence": 0.9 },
        { "review": "security__claude-code-claude-opus-4-8", "severity": "high", "confidence": 0.85 }
      ]
    }
  ],
  "next_steps": [ { "source": "security__...", "steps": ["..."] } ],
  "coverage": { "contributed": 3, "failed": 0 },   // reviews that completed vs. reviews that failed
  "dedup": { "status": "ok", "harness": "...", "reason": null, "warning": null },
  "usage": { "reviews": {...}, "dedup": {...}, "total": { "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0 } }
}
```

**`aeview run` prints a *gate*, not the full report:** it's `report.json` with a top-level `run_id`
added and the result-only detail omitted — `findings[].id`, `next_steps`, `usage`, and the `dedup`
fields other than `status`. The kept fields keep their `report.json` names, so a consumer that reads
only the gate's fields works against both `run` and `result`. The dropped detail (token/cost
accounting, per-review next steps, dedup provenance, per-finding ids) lives in `aeview result`.

**Exit codes** (the loop-until-clean contract):

| Code | Meaning |
|---|---|
| `0` | **approve** — no actionable findings |
| `1` | **needs-attention** — at least one finding to act on |
| `2` | **error** — the run couldn't be trusted (e.g. *every* review failed) |

## Runs, lifecycle & output

Each run is a directory under `~/.aeview/runs/<id>/`:

```
runs/<id>/
  run.json                               # the manifest: scope, roster, dedup plan, state, pid
  bundle/                                # the frozen diff under review
  reviewers/<reviewer>/<instance>/
    review.json                          # that review's findings + status + usage
    review.log                           # raw harness-SDK event stream (JSONL)
  dedup/<instance>/result.json           # the dedup judge's grouping decision
  report.json                            # the merged verdict
```

- **Timeout & fail-fast.** Each harness attempt is bounded by `reviewTimeoutSeconds`; a timed-out
  review fails fast (timeouts aren't retried). A failed review is recorded — the run continues — and
  `resume` re-runs it.
- **Crash recovery.** Runs record their pid; a crashed run is reconciled to `interrupted` and can be
  `resume`d. `aeview status --wait` blocks until a run reaches a terminal state.
- **Retention.** On each `run`, terminal runs outside the newest `keepLast` *and* older than
  `ttlDays` are pruned (`keepLast` is a guaranteed floor).

## Development

```sh
git clone https://github.com/MarlzRana/aeview
cd aeview
uv sync                    # install deps (incl. dev group)
uv run pytest              # the offline test suite (no model calls)
uv run ruff check          # lint
uv run pyright             # type-check
uv run aeview --help
```

## License

[MIT](LICENSE)
