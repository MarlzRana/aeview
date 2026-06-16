---
name: release
description: Publish a new version of aeview to PyPI through its tag-driven Trusted-Publishing GitHub Actions workflow. Use when you're cutting an aeview release — it runs preflight checks, bumps the version, pushes, tags, hands you the PyPI approval gate, then verifies the publish.
disable-model-invocation: true
---

Publish a new version of **aeview** to PyPI via its tag-driven Trusted-Publishing workflow
(`.github/workflows/release.yml`). Run from the aeview repo root. **This is a deliberate, effectively
irreversible action** — a published PyPI version can never be replaced, only yanked — so follow the
steps in order and let the **human** approve the actual publish.

## How aeview's release is wired (already set up — for reference)

- `.github/workflows/release.yml` builds on a `v*` tag and publishes via Trusted Publishing (OIDC,
  `id-token: write`, no stored token), bound to the `pypi` environment. A guard fails the release if
  the tag doesn't match the `pyproject` version.
- PyPI **trusted publisher**: project `aeview`, owner `MarlzRana`, repo `aeview`, workflow
  `release.yml`, environment `pypi` (configured at https://pypi.org/manage/account/publishing/).
- GitHub **`pypi` environment** with the maintainer as a **required reviewer** — the approval gate.

## Release procedure

1. **Preflight.** From the repo root, run the bundled check — it reports branch / sync / version and
   builds + `twine check --strict`s the artifacts before anything irreversible:

   ```bash
   bash ${CLAUDE_SKILL_DIR}/scripts/preflight.sh
   ```

   Resolve anything it flags (dirty tree, behind upstream, build/twine failure) before continuing.

2. **Pick the new version** (semver) and confirm it. It must be greater than aeview's latest published
   version — PyPI is immutable, so a version can never be reused or overwritten.

3. **Bump** `version` in `pyproject.toml`, then run `uv lock` so `uv.lock` records the new version too
   (otherwise the lock drifts behind the release).

4. **Commit** as `chore(release): <version>` — stage **both** `pyproject.toml` and `uv.lock` (put any
   release notes in the body).

5. **Push** main:

   ```bash
   git push origin main
   ```

6. **Tag and push the tag — this triggers the publish. Confirm first.**

   ```bash
   git tag -a v<version> -m "aeview <version>"
   git push origin v<version>
   ```

7. **Hand the approval gate to the maintainer.** The run pauses at the `pypi` environment. Give them
   the run URL (`gh run list --workflow=release.yml` → the run) and ask them to **Review deployments →
   approve**. **Never approve the publish yourself** — that click is the maintainer's go/no-go on the
   permanent upload.

8. **Verify after it publishes.** Watch the run (`gh run watch <run-id>`); on success, confirm:
   - `https://pypi.org/pypi/aeview/<version>/json` returns 200 (the plain `…/aeview/json` "latest" can
     lag a minute behind the CDN);
   - a clean install works: `uv tool install --prerelease=allow aeview && aeview --version` (mind the
     prerelease flag — see Gotchas).

## Gotchas

- **Immutable index.** A published version's files *and* its long-description (the README rendered on
  https://pypi.org/project/aeview/) are frozen. To change the PyPI page text, publish a new version.
- **uv needs `--prerelease=allow`.** aeview pulls a prerelease Codex runtime (`openai-codex` is in beta
  and pins an alpha `openai-codex-cli-bin`), so `uv tool install` / `uv pip install` need
  `--prerelease=allow`; `pip` / `pipx` allow it automatically. Verify the install with the flag. This
  goes away once `openai-codex` ships stable.
- **A failed run is safe.** If build, `twine check`, or the OIDC publish fails, nothing is uploaded —
  fix and re-release (delete + re-push the tag, or bump to the next version).
- **Tag ↔ version lockstep.** The workflow guards `tag == pyproject version`; keep them aligned.
