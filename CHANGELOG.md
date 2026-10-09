# Changelog

All notable changes to the Kardashev Index are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) as described in [docs/RELEASING.md](docs/RELEASING.md).

Component versions (pipeline, prompts, weights, rubrics) are tracked separately in
`app/pipeline/methodology.py` and `app/pipeline/prompts.py`. Each run records the versions it was
scored with. Whenever a component version changes, the entry here says so.

## [Unreleased]

## [0.5.1] - Unreleased (untagged; tag after merge with owner approval)

PATCH: display only. No scoring, ranking or component-version changes
(pipeline-v2.3, prompts-v2.2, weights-v1, rubrics-v1 unchanged).

### Fixed
- Unranked runs never show a numeric Index on public pages. Run history shows "Unranked" (with the
  not-ranked reason as a tooltip) instead of the raw number (e.g. Anduril's "10.0 nr"); the entity
  header gauge reads "Unranked".
- Index deltas compare ranked runs only: each ranked run is compared with the latest earlier ranked
  run (e.g. "+0.6 vs N-2"), and unranked runs get no delta. Tesla's "+3.4 vs N-1" (ranked 4.6 vs an
  unranked 1.2) no longer appears.
- The Index sparkline plots ranked runs only (unranked runs are gaps).
- Admin views label an unranked run's number "raw · unranked". The Hermes JSON endpoints keep the raw
  `index_score` next to `ranked` (see `docs/API.md`).

## [0.5.0] - Unreleased (untagged; tag after merge with owner approval)

MINOR: ranking methodology change. Component version: **pipeline-v2.2 → pipeline-v2.3**
(prompts-v2.2, weights-v1, rubrics-v1 unchanged; no score, weight or anchor changes).

### Added
- Licensing: `LICENSE` (MIT, code), `DATA-LICENSE.md` (CC BY 4.0 for scores, data and methodology
  text; third-party quotes and figures excluded), `NOTICE.md` (htmx 0BSD, Google Fonts OFL 1.1,
  project images), README "License" section, footer "Scores licensed CC BY 4.0" line and an
  `/about#license` section (#14).
- JSON: runs carry `ranked_at_run` (the stored verdict) next to `ranked` (the effective one).

### Changed
- Ranking quality gate. An entity is ranked only if it passes the existing coverage rule (at least
  60% of the weight scored with at least 30% measured, or the energy-undisclosed rule) **and** both:
  - more than 40% of the *scored* weight comes from measured categories (`RANK_MIN_MEASURED_SHARE`,
    default 0.40), and
  - confidence is above 20% on the 0-100% scale shown on the site (stored 0-1, so > 0.20;
    `RANK_MIN_CONFIDENCE`, default 0.20). For energy-undisclosed runs this is the reduced (x0.8)
    confidence. Both comparisons are strict.
- The gate is recomputed at display time for every stored run from its stored coverage, confidence
  and measured coverage (`app/eligibility.py`). Stored `ranked` values and scores are not rewritten
  and there is no migration; new runs also store the gated verdict. Leaderboard, entity pages,
  history, recent judgments and the publication rule use the effective status.
- The daily sweep still uses each run's stored verdict, so this change does not trigger re-runs.
- `/methodology` (ranking rules), the not-ranked notice and `docs/API.md` describe the new rule.

## [0.4.0] - 2026-10-09

MINOR: new public pages and endpoints. Includes everything merged since 0.3.0 (#5, #6, #7 and
this PR). No scoring, ranking or component-version changes
(pipeline-v2.2, prompts-v2.2, weights-v1, rubrics-v1 unchanged).

### Added
- `/about`: what the index is, how scores are made (AI-assisted; xAI Grok), a conflict-of-interest
  disclosure (the scoring model's maker, xAI, is also ranked), not-investment-advice notice and a
  corrections section.
- `/privacy`: the actual data practices. No analytics or tracking cookies; a session cookie only
  for signed-in admins; suggestion form data; IP addresses held in memory only for rate limiting;
  Render request logs; Google Fonts; xAI API (company data only).
- Methodology "Disclosures" section: AI assistance, conflict of interest, measured vs opinion share,
  not investment advice.
- Measured share of the scored weight is shown next to each Index on the leaderboard
  ("meas NN%") and in each entity's Index composition. Display only.
- Footer: not-investment-advice line with the AI/conflict disclosure, and links to About,
  Corrections and Privacy.
- `CONTACT_EMAIL` env var: the corrections/privacy contact. When it's unset or invalid, pages show
  placeholder text instead of an address.
### Added
- `PUBLIC_BASE_URL`: the canonical public origin. When set, other hosts (including
  `*.onrender.com`) are redirected to it (`301` for GET/HEAD, `308` otherwise; `/health` and
  `/internal/*` are exempt). Canonical, `og:url`, `og:image` and the sitemap use it. Unset = no-op.
- Every page has an absolute `<link rel="canonical">` and `og:url`. Search results (`/?q=`) point to
  `/`, and historical run pages point to the company page.
- `/robots.txt` (disallows `/admin`, `/internal`, historical run views; links the sitemap),
  `/sitemap.xml` (public pages plus every company, with `lastmod` from the current run) and
  `/favicon.ico`.

### Security
- Upgraded dependencies with known advisories (pip-audit: 68 advisories → 0). FastAPI 0.115 → 0.143
  with Starlette pinned at 1.7.0 (was 0.38.6); python-multipart 0.0.9 → 0.0.32; pypdf 5.1 → 6.20;
  jinja2 3.1.6; lxml 6.1.3; python-dotenv 1.2.4. Templates now use Starlette 1.x's
  `TemplateResponse(request, name, context)` signature. (#6; this entry was dropped from `main` when
  #7 was merged and is restored here.)
- Dependabot version updates (`.github/dependabot.yml`): pip and GitHub Actions, weekly, grouped.
- Security headers on every response: CSP (`script-src 'self'`, `frame-ancestors 'none'`),
  X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy, COOP, and HSTS over HTTPS only
  (`HSTS_MAX_AGE`, default 1 day; `HSTS_INCLUDE_SUBDOMAINS` opt-in).
- htmx is vendored (`static/vendor/htmx-1.9.12.min.js`, SRI-pinned) instead of loaded from unpkg.
  Inline `onsubmit` confirms moved to `static/js/app.js` (`data-confirm`).
- Admin session cookie: `Secure` in production, `HttpOnly`, `SameSite=Lax`, 12-hour lifetime. The
  session is renewed on login.
- `SECRET_KEY` is required in production (`RENDER=true` or `APP_ENV=production`). There is no more
  `dev-secret` fallback; local development uses a random per-process key.
- Admin login throttle: 5 failures per IP in 15 min locks that IP for 15 min; 50 failures overall
  pause all logins for 15 min. Responses are `429` with `Retry-After`, and failures are logged.
- Cross-origin `POST`/`PUT`/`PATCH`/`DELETE` to `/admin/*` and `/internal/*` are refused with `403`
  (Origin/Referer must match the host; header-less API clients such as Hermes are unaffected).

### Changed
- Render now deploys a `main` commit only after its GitHub checks pass (`autoDeployTrigger: checksPass`)
  and uses `/health` as its health check (`healthCheckPath`). The outdated `env: python` /
  `autoDeploy: true` keys are replaced by `runtime: python` / `autoDeployTrigger`.
- `/health` returns **503** when the instance can't query the database. It used to return 200 with
  `"status": "degraded"`. A dead or stalled worker still returns 200 (`"status": "degraded"`, worker
  state in the body), so the worker can never cause restarts or failed deploys.

## [0.3.0] - 2026-10-08

### Added
- `app/version.py` is now the single source of truth for the app version. `/health`, page
  footers and `pyproject.toml` all read it.
- This changelog, `docs/RELEASING.md` (branching, versioning, tagging, rollback) and a pull-request
  template.
- A CI check (`version-check`, PRs only) that fails when a pipeline/prompt/weights/rubric version
  or a database migration changes without an app version bump and a changelog entry.
- A `test-postgres` CI job that runs the full suite against Postgres 17, the production engine.
- `requirements-dev.txt` pins the dev tools (ruff 0.16.10, pytest 9.1.1). CI installs from it.

### Changed
- Python pinned to 3.12.15 everywhere: `.python-version` (single source for CI), `PYTHON_VERSION`
  in `render.yaml` (production previously built on Render's default, 3.14.3), the Dockerfile base
  image, and `requires-python` in `pyproject.toml`.
- The displayed version format is now `v0.3.0`; it used to be `v0.2`.
- `.env.example` documents every v2.2 setting (EDGAR user agent, alert webhook, sweep, retry and
  budget settings). The obsolete `OPENAI_API_KEY` is gone.
- Dockerfile and docker-compose.yml are fixed as a local, production-like stack (Postgres 17,
  non-root, migrations on start). Render does not use them.
- `main` is protected: changes go through PRs, squash merge only, and all four CI checks are
  required.

### Removed
- The stale `Procfile` (Render starts the app from `render.yaml`), the finished
  `PUBLIC-PAGES-PLAN.md`, and a committed `.pyc` file.

Component versions unchanged: pipeline-v2.2 · prompts-v2.2 · weights-v1 · rubrics-v1. Latest migration: 0005.

## [0.2.0] - 2026-10-08

Baseline tag for everything shipped after the initial release, up to and including pipeline v2.2.

### Added
- Measured + judged v2 scoring pipeline: figures are verified against the cited sources, SEC EDGAR
  filings are used, and each run has a hard budget cap.
- DB-backed in-process run queue (worker). Approve and re-run return `202`.
- Judgment runs, stages, sources, evidence and metrics tables (migrations 0002–0005). Every run
  records the code version.
- Public run history, admin run list and run detail with live re-run feedback, and a per-figure
  verification view.
- Methodology page rendered from the same module that computes the scores.
- Reliability (v2.2): stage checkpoints, automatic retries with backoff, fetch fallbacks, EDGAR
  User-Agent, identity pinning, failure alerts (webhook), a stale-run sweep and an
  "energy undisclosed" ranking marker.
- e/acc design system restyle. Mobile layout fixes (360–768px, 44px tap targets).
- Golden-set, reliability and v2.2 rule tests. CI fails on test failures.

### Changed
- Metric semantics are defined and enforced (pipeline-v2.2, prompts-v2.2).
- A run that ends with insufficient data never replaces better published data (migration 0004).

### Fixed
- Admin form actions, suggestion model fields and dual auth (admin session or Hermes key) on
  internal routes.
- Figure verification from PDF tables and real-world quote formatting.

## [0.1.0] - 2026-10-08

### Added
- Initial production release: public leaderboard and company pages, suggestions, admin
  approve/deny and Postgres via Alembic (migration 0001).

[Unreleased]: https://github.com/wilfullyapt/kardashev-index/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/wilfullyapt/kardashev-index/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/wilfullyapt/kardashev-index/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/wilfullyapt/kardashev-index/releases/tag/v0.1.0
