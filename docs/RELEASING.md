# Releasing the Kardashev Index

How changes reach production, how versions are chosen and how to roll back.

## Branching: trunk-based
- `main` is the only long-lived branch. Render auto-deploys every `main` commit whose CI passes to
  https://kardashev-index.onrender.com.
- All work goes through a short-lived branch (`feat/…`, `fix/…`, `chore/…`, `docs/…`) and a pull
  request. Direct pushes to `main` are blocked by the `main` ruleset.
- Before a PR can merge, the required checks (`lint`, `test`, `test-postgres`, `version-check`) must
  pass and the branch must be up to date with `main` (strict).
- **Squash merge only.** One PR becomes one commit on `main`, and its title follows Conventional
  Commits (`feat: …`, `fix: …`, `chore: …`). History stays linear (enforced).
- PRs that change nothing the running app uses (docs, CI, dev tooling) may put `[skip render]` in
  the squash title. Render then skips the deploy.
- Delete the branch after merging.

## Deploys (Render)
The web service is managed by the Blueprint in `render.yaml`. The deploy flow is:

1. A PR merges (squash) into `main`. GitHub Actions runs `lint`, `test` and `test-postgres` on the
   new commit. `version-check` is PR-only and shows as *skipped*, which Render counts as passed.
2. **`autoDeployTrigger: checksPass`**: Render starts the deploy only after all of the commit's
   checks finish successfully. If any check fails, nothing deploys. Fix it in a new PR.
   - If a commit has **zero** checks, Render does not deploy it. Don't use `[skip ci]` on `main`,
     because it would block the deploy. To skip a deploy deliberately, use `[skip render]`.
3. Build: `pip install --only-binary :all: -r requirements.txt && alembic upgrade head` on Python
   3.12.15 (pinned by `PYTHON_VERSION` and `.python-version`).
4. **`healthCheckPath: /health`**: traffic switches to the new instance only after `/health`
   answers 2xx. If it doesn't within 15 minutes, Render cancels the deploy and keeps the old one.
   On a running instance, 60 s of failed checks makes Render restart it.
   - `/health` is **200** whenever the web process can query the database, and **503** when it
     can't. Worker state (`worker.alive`, queue, retries, last run) is informational and never
     changes the HTTP status. A stalled worker shows `"status": "degraded"` with a 200 response.
5. Verify: `curl -s https://kardashev-index.onrender.com/health`. `code_version` must equal the
   merge commit SHA, and `version` must equal `app/version.py`.

Manual deploys (dashboard *Manual Deploy → Deploy latest commit*) still work and ignore checks.
Avoid *Deploy a specific commit*: it turns auto-deploy off (see Rollback).

## Versioning: SemVer
The app version lives in **one place**: `app/version.py` (`__version__ = "X.Y.Z"`, no leading `v`).
`/health`, page footers and `pyproject.toml` all read it. Bump it **in the PR that makes the
change**, together with a `CHANGELOG.md` entry.

The scoring components have their own versions, which are recorded on every run:

| Constant | File | Bump it when… |
|---|---|---|
| `PIPELINE_VERSION` | `app/pipeline/methodology.py` | stage logic, extraction/verification or the math changes |
| `WEIGHTS_VERSION` | `app/pipeline/methodology.py` | category weights or normalization anchors change |
| `RUBRIC_VERSION` | `app/pipeline/methodology.py` | judged-category rubrics change |
| `PROMPT_VERSION` | `app/pipeline/prompts.py` | any prompt text or output schema changes |

### Choosing the app version bump
Before 1.0.0, a MINOR bump was used where 1.x needs a MAJOR one. From **1.0.0** (the public launch
baseline) standard SemVer applies: the MAJOR row below means a MAJOR bump.

| Change | Bump |
|---|---|
| Any component version above changes (pipeline, prompts, weights, rubrics), i.e. scores can mean something different | **MINOR** (at least) |
| New Alembic migration | **MINOR** (at least) |
| New user-visible feature or new/changed endpoint | MINOR |
| Breaking change to the internal API (`/internal/*`, Hermes) or the `/health` contract, or a migration that isn't backward compatible | MAJOR (MINOR while on 0.x, called out under **Breaking** in the changelog) |
| Bug fix or copy/UI tweak that doesn't change what scores mean | PATCH |
| Docs, CI, tests, dev tooling only | none needed (record under `[Unreleased]` if worth noting) |

The `version-check` CI job (PRs only, `scripts/check_version_bump.py`) enforces the first two rows.
It fails if a component version changes or a migration is added without at least a MINOR bump of
`app/version.py` and a matching `## [X.Y.Z]` section in `CHANGELOG.md`.

### Migrations
- Use expand/contract. A migration has to work with both the old and the new code, because Render
  runs `alembic upgrade head` before the new code takes traffic, and a rollback runs old code
  against the new schema.
- Take a `pg_dump` before any migration that rewrites or deletes data.

## Cutting a release
Several PRs can accumulate before a release. Releases are tagged from `main` **after merge, with
owner approval**:

1. Make sure `CHANGELOG.md` has a section for the version in `app/version.py`. Move items out of
   `[Unreleased]` and set the date.
2. Confirm CI is green on the `main` commit and the Render deploy is live
   (`curl -s https://kardashev-index.onrender.com/health` shows the new version).
3. Tag that commit with an annotated tag that matches the version exactly:
   ```bash
   git tag -a v0.3.0 -m "v0.3.0" <sha> && git push origin v0.3.0
   ```
   The `release tags v*` ruleset blocks deleting, moving or force-updating `v*` tags, so get the
   tag right the first time.
4. Create the GitHub Release from the tag, with the changelog section as the notes:
   ```bash
   gh release create v0.3.0 --title "v0.3.0" --notes-file <(sed -n '/^## \[0.3.0\]/,/^## \[/p' CHANGELOG.md | sed '$d')
   ```

## Rollback
1. **Fastest:** in the Render dashboard, open the service → *Events*/*Deploys* and use **Rollback**
   on the last good deploy. Render redeploys that build without touching `main`. The next
   `main` commit that passes CI deploys again (auto-deploy is `checksPass`), so fix forward
   promptly. Check under *Settings → Auto-Deploy* that auto-deploy is still "After CI Checks Pass"
   afterwards.
2. **Fix forward / revert:** open a PR that reverts the bad squash commit
   (`git revert <sha>`) and merge it through the normal checks.
3. **Migrations are not undone automatically.** Expand/contract keeps old code working on the new
   schema. Only run `alembic downgrade` deliberately, after a `pg_dump`.
4. Never delete or move a release tag. Ship a new PATCH version instead.
