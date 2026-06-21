[`README.md`](README.md) is authoritative for the user-facing CLI surface (scopes, flags, config
keys, report shape) — don't duplicate it here. This file is the developer context: the architecture,
the load-bearing invariants, the design *why*, and how we work. On any conflict about user-facing
behavior, the code and README win.

## What aeview is

A **bring-your-own-harness, multi-agent code-review CLI**. It runs the *same* change past several
**reviewers** — each a prompt checked into the repo — across several agent **harnesses** (Claude
Code, Codex, Copilot) in parallel, then merges one deduplicated verdict (`report.json` + an exit
code: `0` approve / `1` needs-attention / `2` error).

The bet: a *reviewer* is a versioned, diffable artifact (a prompt + the harnesses it runs on), the
prompt is decoupled from the harness (harnesses are swappable adapters), and a panel of independent
models catches what any single model misses. It's a composable primitive — an agent's closeout loop
can `aeview run` and loop until the exit code is clean.

Python ≥3.14 CLI, packaged with `uv`, published to PyPI (early/alpha: `0.0.x`). Single maintainer
(**MarlzRana**); repo `https://github.com/MarlzRana/aeview`.

## Vocabulary (load-bearing — use these terms precisely)

- **reviewer** — a prompt (`.aeview/reviewers/<name>/REVIEWER.md`, YAML frontmatter + body) plus the
  set of harness instances to run it on. Dir name must equal frontmatter `name`.
- **harness** — a runtime: `claude-code`, `codex`, `copilot`. **harness instance** = `{harness,
  model, thinking?}`.
- **review** — one reviewer × one harness instance: the atomic unit of work. Id `<reviewer>__<harness>-<model>`.
- **roster** — the full cross-product of requested reviewers × their harness instances. Frozen in `run.json`.
- **run** — one `aeview` invocation; a UUID dir under `~/.aeview/runs/<id>/`. **run-id** is printed
  immediately so `status`/`resume`/`result` work after a kill or from another terminal.
- **scope** — what diff is reviewed (`--scope <type[:value]>`; types in the README). Diff is always
  computed from the **repo root**; cwd only affects reviewer walk-up.
- **bundle** — the materialized diff under review; **inline** (small, frozen) vs **self-collect**
  (large → hand the harness read-only git to fetch it). Built once per run.
- **fan-out** — running the whole roster concurrently (unbounded).
- **finding** — one issue: title/body/severity/category/confidence/location/recommendation.
- **dedup judge** — the harness (`deduplicationHarness`, prompt `DEDUPLICATION.md`) that groups
  duplicate findings. Returns **id-groups only**, never content. **survivor** = the one finding kept
  verbatim per group; **agreement** = group size; **sources[]** = per-merged-finding provenance.
- **gate vs report** — `aeview run` prints a slim **gate** (verdict + trimmed findings + `run_id`);
  `aeview result` / `report.json` is the **full** Report. See README "The report & exit codes".

## How a run works (data flow)

`cli.run` → `load_settings()`/`ensure_seeded()` → `parse_scope` → **resolve.py** (walk-up reviewer
discovery, first-match-wins) → **scope.py** (turn `--scope` into a git diff) → **ignore.py**
(`.aeviewignore` filtering) → **activate.py** (auto-mode roster = `default` ∪ path-matched reviewers)
→ **bundle.py** (inline vs self-collect, 256 KB threshold) → **prompt.py** (compose, deterministic) →
**fanout.py** (`asyncio.gather`, one task per roster entry, retry transients ×3, never raises) →
**merge.py** (`merge_reviews`: pool findings → conditionally `run_dedup` → apply groups or raw-union
fallback → sort → verdict/summary/coverage/usage) → **report.py** (`run_gate_dict` / `render_human`,
`exit_code`) → **runstore.py** persists throughout. Returns `(run_id, Report)`; exit `0/1/2`.

`resume` re-runs non-`done` reviews against the **frozen** bundle + prompts + dedup plan. `status`
reads the manifest (liveness-adjusted) and renders progress; `--wait` polls to terminal + adopts the code.

## Code map (`src/aeview/`)

| Module | Owns |
|---|---|
| `cli.py` | Typer app; all subcommands; the `_Plan` (dry-run-safe) + `_execute` orchestration; query commands. No harness/diff/storage detail. |
| `config.py` | `~/.aeview` paths, self-seeding (`SEED_FILES`, write-if-absent), `Settings`/`Retention`/`HarnessInstance` (the **camelCase** boundary), `load_dedup_prompt`, `split_frontmatter`. |
| `resolve.py` | Reviewer walk-up discovery, `REVIEWER.md` frontmatter parse/validate, `build_roster`. |
| `scope.py` | Diff acquisition: scope grammar + all 8 resolvers, base resolution, conflict detection. Forces git diff config (`_GIT_BASE`). |
| `bundle.py` | Inline-vs-self-collect decision (`build_bundle`). |
| `ignore.py` | `.aeviewignore` (gitignore semantics via `pathspec`), diff-block filtering, `changed_paths`. |
| `activate.py` | Auto-activation (`PurePath.full_match` literal globs — *not* the gitignore engine). |
| `prompt.py` | Deterministic prompt composition (reviewer dir anchor + body + read-only guard + change section). |
| `fanout.py` | Concurrent execution; per-review retry/backoff; workers are sole writers of their `review.json` and never let exceptions escape `gather`. |
| `dedup.py` | The dedup harness call; findings framed as **untrusted data**; persists `DedupResult`. |
| `merge.py` | Post-fanout assembly: pool → dedup/apply or raw-union → sort → `Report`. |
| `report.py` | Exit-code contract, `run_gate_dict` (the gate shape), `render_human`. No I/O. |
| `runstore.py` | On-disk run layout, atomic `tmp→os.replace` writes, run enumeration, prune/retention, pid/crash reconciliation. |
| `schema.py` | All canonical Pydantic models (snake_case, `extra="forbid"`) + JSON-schema helpers (`make_strict_schema`). |
| `doctor.py` | `aeview doctor` preflight (reviewers, harness SDK/binary/auth, `gh`). |
| `process.py` | Subprocess helpers (`run_sync`/`run_async`, `TIMED_OUT=124`). |
| `harness/base.py` | The `Adapter` Protocol, `AdapterError(transient)`, `SchemaSupport`, `get_adapter` registry. |
| `harness/{claude_code,codex,copilot}.py` | The three adapters (see Harnesses). |
| `harness/eventlog.py` | Best-effort live JSONL event log per invocation (never raises). |

### Load-bearing invariants (don't break these)

- **Directory name is authoritative** for a run, not the manifest's self-declared `run_id`; reads/
  deletes always use the enumerated dir path (guards against a corrupt/hostile `run.json`).
- **Resume is byte-identical**: the prompt and the dedup-harness plan are **frozen in `run.json`** at
  run start; resume never recomposes or re-reads live `settings.json` for them (binary overrides are read live).
- **All writes are atomic** (`atomic_write_text` → tmp then `os.replace`). SIGKILL can't leave a torn file.
- **Worker-sole-writer**: only the fan-out worker writes its `review.json`; only the orchestrator
  writes `run.json`/`report.json`.
- **`reconcile_interrupted` runs before `prune`** so crashed `running` runs become terminal first.
- **Dedup input is untrusted**: the pool is fenced as data; output is schema-constrained to id-groups,
  so a crafted finding can at worst over-merge — never inject output.
- **Self-seeding on every invocation** (write-if-absent) — no install hook seeds anything.

## Storage & state (`~/.aeview/`)

`settings.json` (camelCase), `DEDUPLICATION.md` (the user-editable dedup prompt), `reviewers/`
(incl. the seeded `default` reviewer), `runs/<uuid>/`. Run dir: `run.json`, `report.json`,
`bundle/`, `reviewers/<reviewer>/prompt.md` + `<instance>/{review.json,review.log}`,
`dedup/<instance>/{prompt.md,input.json,result.json,dedup.log}`. Persistence is **file-based on
purpose** — atomic per-review writes are what make a killed run survive + resumable. No SQLite/DB.

**camelCase ↔ snake_case**: only `config.py` models (`Settings`/`Retention`/`HarnessInstance`) use
`alias_generator=to_camel` + `populate_by_name=True` — that's the *user-facing* `settings.json`
surface. Every `schema.py` model is snake_case (`extra="forbid"`); all run artifacts are snake_case.

## Harnesses

| harness | SDK | schemaSupport | read-only enforcement |
|---|---|---|---|
| `claude-code` | `claude-agent-sdk` | `validated` (validate-and-reprompt) | OS sandbox (`denyWrite:["/"]`) + `permission_mode=dontAsk` + disallow Edit/Write |
| `codex` | `openai-codex` | `constrained` (decoding, strict schema) | native `Sandbox.read_only` + `ApprovalMode.deny_all` |
| `copilot` | `github-copilot-sdk` | `prompt` (schema embedded, re-prompt once) | deny-by-default permission handler (approve `read` only) |

Each SDK ships a **pinned binary** (insulated from the user's own CLI upgrades; override via
`overrideHarnessBinaries`). Every adapter normalizes *all* exceptions to `AdapterError` and tears
down cleanly in `finally` (codex/copilot each run on an **isolated daemon thread + own event loop**
to avoid pool starvation; copilot also `delete_session`s to avoid on-disk session leaks). Adding a
harness = one new adapter implementing the `base.py` Protocol; nothing else changes.

## Tech stack, tooling & commands

- **Python ≥3.14**, **uv** (`uv_build` backend), macOS/Linux. Entry point `aeview = aeview.cli:app`.
- **ruff** (line-length 100; `E,F,I,UP,B,SIM`) is the lint+format gate — *not* black/isort.
- **pyright** (`standard` mode, `src`+`tests`, python 3.14).
- **pytest** (`asyncio_mode=auto`; `smoke` marker for live tests). Tier-1 = offline (SDK-boundary
  stubs, throwaway git repos, no model calls) and runs in CI; Tier-2 = `pytest -m smoke` (live, local-only).

```sh
uv sync                 # deps incl. dev group
uv run pytest           # offline Tier-1 suite
uv run ruff check       # lint  (uv run ruff format  to format)
uv run pyright          # type-check
uv run aeview --help
uv run pytest -m smoke  # Tier-2 live smoke (local only)
```

Known toolchain drift: newer ruff/pyright flag a few **untouched** test files (`test_query`,
`test_retention`, `test_harness`). Treat as pre-existing **baseline** — don't fix it mixed into
feature work, and keep your *own* touched files clean. Never edit baseline/snapshot/expected files to
silence a check.

## Code conventions (match these)

- `from __future__ import annotations` everywhere; strict typing, avoid `Any`.
- **`Literal` aliases** for closed enums (not `Enum`); closed shapes via Pydantic `extra="forbid"`.
- **Pydantic `BaseModel`** for anything serialized to disk; **`@dataclass(slots=True)`** for
  in-memory-only structures (never serialized). This split is firm.
- Errors are exceptions for control flow; adapters normalize to `AdapterError`; workers never let
  exceptions escape `gather`.
- Lean code: prefer deleting branches/modes to adding them; helpers must pay rent. Comments explain
  *why* (the invariant a branch protects), never narrate syntax.
- Respect module responsibility boundaries above (e.g. `merge.py` does no I/O; `report.py` writes nothing).

## How we work (development workflow)

**Increment loop**: implement → commit → **dogfood to convergence** → resolve → repeat. Each
increment is its own slice.

**Dogfood to convergence** (see `~/artifacts/aeview-convergence.md`): run the full reviewer panel
over the increment's commits, triage findings, fix what's worth fixing, **commit each round
incrementally**, re-run. Converged = a fresh cycle surfaces **no new *actionable* findings** (not
"zero findings"). Bounds: **min 2 cycles, max 5** (stop at 5 and report what's open). For committed
work, dogfood `--scope commits:<…>` / `range:<base>..HEAD`.

**The context-blind-reviewer filter (critical):** the panel sees only the code, not the project
context in your head (single user, unreleased, decisions already made, deferrals). A finding can be
*correct about the code* yet premised on a scenario that can't occur (e.g. "existing users would
break" — there are none; "add a migration for the old format" — migration was declined). **Filter
every finding against this hidden context and ignore (with a recorded reason) those premised on a
non-existent scenario — even high-agreement ones** (agreement just means the reviewers share a blind
spot). Building to satisfy them is over-engineering. When genuinely unsure, flag it, don't guess.

**Hard gates each increment (independent of convergence):** ruff clean, pyright clean on touched
files, pytest green.

**Commits:** conventional-commit-style prefixes (`feat`/`fix`/`refactor`/`docs`/`chore`/`ci`/`perf`/
`nit`), imperative, concise; group logically; end every commit with
`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Use the `/commit` skill's message
guidance. Commit **directly to `main`** (no PR flow for the maintainer). **The maintainer runs
pushes and releases** — don't push unless asked or explicitly delegated.

**Asking:** use **AskUserQuestion** for genuine forks until you're ~95% confident; pick sensible
defaults for everything else and say what you chose. Verify external/dependency behavior empirically
— don't guess at SDK/API/CLI behavior.

**Releases:** tag-driven PyPI Trusted Publishing (`.github/workflows/release.yml`: `v*` tag → build →
`twine check` → OIDC publish, gated on a `pypi` environment manual approval). Bump `pyproject` +
`uv lock` → commit both → push → tag → **maintainer approves the gate (never self-approve)**. Use the
`/release` skill, which automates this.

## Constraints & deliberate non-goals

- **Prerelease floor:** aeview's dep closure pulls a *prerelease* Codex runtime (`openai-codex` is
  beta and pins an alpha `openai-codex-cli-bin` binary wheel). Consequence: `uv tool install` / `uv
  pip install` need `--prerelease=allow`; `pip`/`pipx` don't. Goes away when `openai-codex` ships
  stable. Don't try to "fix" the prerelease deps.
- **Don't reverse these intentional decisions** (from the dossier): no scope filter (reviewers are
  trusted to stay on-target — prompt-trust, not autoreview's mechanical post-filter); no
  `--prompt`/`--prompt-file` (a reviewer *is* its `REVIEWER.md`); verdict/summary/next_steps are
  computed **mechanically**, no LLM synthesis; the survivor finding is kept **verbatim** (no
  severity/range stitching); `next_steps` stay **per-review** (not flattened); no broker/daemon;
  dedup is **precision-over-recall** (a wrong merge hides a real finding; a wrong split is cheap).
- **v1 non-goals:** no path filtering (`--paths`/`--exclude`), no Windows. **Deferred:** I6b-2
  (`--detach`/`cancel` + concurrent-run lock), a Homebrew tap (until `openai-codex` is stable — the
  prerelease floor makes a formula high-maintenance now), PR CI, and Tier-2 smoke in CI.

## Status & roadmap

Increments **I1–I12 shipped**; the SDK migration **N5 (claude/codex/copilot) is complete**; aeview is
**published on PyPI** (`0.0.x`) via the release workflow. Remaining backlog is post-release: Homebrew
tap (deferred), PR CI, Tier-2 smoke, and dropping the prerelease floor once `openai-codex` ships
stable. Build history + per-increment decisions live in `implementation_log.local.md`.

## Skills shipped in this repo (`.agents/skills/`)

`aeview` (run a review), `aeview-install` (bootstrap CLI + skills), `aeview-pr`, `aeview-loop`,
`aeview-commits`, `aeview-effective-pr` — installed by users via `npx skills add MarlzRana/aeview`.
Plus `release` — a **maintainer-only** runbook (not for end-user install). Each has a
`.claude/skills/<name>` symlink → `../../.agents/skills/<name>`.

## Further reading (local, not committed)

These hold the deep rationale and history; they live on the maintainer's machine, not in the repo:

- **`~/artifacts/aeview-comparison.html`** — the design dossier (the *why*: thesis, the five locked
  commitments, rejected alternatives, the increment plan). **Design intent, and partly stale on
  facts** (it predates shipping — e.g. it says Python 3.11 / `harnessBinaries`; the shipped truth is
  Python 3.14 / `overrideHarnessBinaries`). Code, `README.md`, and `pyproject.toml` are authoritative for facts.
- **`implementation_log.local.md`** — running build log: what each increment shipped, decisions,
  deferrals, the live backlog. Gitignored (`*.local.md`).
- **`~/artifacts/aeview-convergence.md`** — the full dogfood-to-convergence practice definition.
