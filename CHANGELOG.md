# Changelog

All notable changes to the Kardashev Index are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) as described in [docs/RELEASING.md](docs/RELEASING.md).

Component versions (pipeline, prompts, weights, rubrics) are tracked separately in
`app/pipeline/methodology.py` and `app/pipeline/prompts.py`. Each run records the versions it was
scored with. Whenever a component version changes, the entry here says so.

## [Unreleased]

## [1.0.0] — Public launch baseline

Unreleased (untagged; to be tagged `v1.0.0` after merge, with owner approval). No functional change
from 0.15.0: same pipeline-v2.9, prompts-v2.5, weights-v1, rubrics-v1, migrations 0001–0007.
From 1.0.0, standard SemVer applies: breaking changes to `/internal/*`, the `/health` contract or
non-backward-compatible migrations are MAJOR (see docs/RELEASING.md).

### Added
- `HANDOFF.md`: operating manual for a new owner or operator (architecture, deploys, every
  environment variable, operations, costs, licensing, launch checklist, known limitations, accounts
  to transfer). Linked from the README.

### Pre-1.0 summary (details in the entries below; history kept as written)
- **0.1**: public leaderboard and company pages, suggestions, admin approve/deny, Postgres via Alembic.
- **0.2**: measured + judged v2 pipeline with verified figures and SEC EDGAR, DB-backed in-process
  worker, run history and admin run views, reliability (checkpoints, retries, fetch fallbacks).
- **0.3**: release process: SemVer in `app/version.py`, this changelog, RELEASING.md, the
  `version-check` and `test-postgres` CI jobs, Python 3.12.15 pinned, protected `main`, CI-gated
  Render deploys.
- **0.4**: `/about`, `/privacy`, disclosures, `CONTACT_EMAIL`, `PUBLIC_BASE_URL` canonical host,
  robots.txt and sitemap, security headers, login throttle, dependency upgrades.
- **0.5**: licensing (MIT code; CC BY 4.0 data from v0.5.0) and the ranking quality gate
  (pipeline-v2.3); 0.5.1 shows unranked runs without a numeric Index.
- **0.6–0.9**: scoring fixes: future capacity is not counted as operating (0.6), "energy source
  couldn't be read" keeps a run unranked with a retry (0.7), compact energy data sources are
  preferred (0.8), over-cap PDFs are read in a capped child process and unlabelled energy rows are
  ambiguous (0.9).
- **0.10**: admin retraction, newer-pipeline-wins, current run computed at read time.
- **0.11–0.13**: a Wayback fallback that works (CDX, block pages, rate limits, attempts recorded),
  report-PDF discovery, and a tighter energy-unreadable rule (own disclosure only).
- **0.14**: immediate nudged re-ask on empty extraction; `/health` `queued_now` / `scheduled`.
- **0.15**: data license CC BY-NC 4.0 from 0.15.0; earlier CC BY 4.0 grants (v0.5.0–v0.14.0) stand.

## [0.15.0] - Unreleased (untagged; tag after merge with owner approval)

MINOR: data-license change (what users may do with published content changes, so not a PATCH copy
tweak; see docs/RELEASING.md). No component version changes (pipeline-v2.9, prompts-v2.5,
weights-v1, rubrics-v1); no migration. Code stays MIT.

### Changed
- **Data license: CC BY 4.0 → CC BY-NC 4.0** for scores, data and methodology text published from
  0.15.0 onward ([legal code](https://creativecommons.org/licenses/by-nc/4.0/legalcode)). Content
  published by v0.5.0–v0.14.0 was CC BY 4.0; those grants are irrevocable and stand. Commercial use,
  bulk or automated access and resale need a separate license (contact via `CONTACT_EMAIL`, or the
  placeholder until it is set). Facts and figures quoted from company sources are not covered.
  Updated: `DATA-LICENSE.md`, README, NOTICE, the site footer ("Scores CC BY-NC 4.0 · Code MIT ·
  Commercial licensing") and `/about#license` (with `#earlier-license` and `#commercial-licensing`).

## [0.14.0] - Unreleased (untagged; tag after merge with owner approval)

MINOR: extraction retry behaviour and `/health` fields. Component version: **prompts-v2.5** (pipeline,
weights and rubrics unchanged). PR D of `ki-pipeline-issues-2.md`; independent of PRs A–C (numbered as
if it merges after them; renumbered on update if it merges first).

### Changed
- **Empty extraction**: the model sometimes answers `{"figures": [], "claims": []}` with a normal
  finish (Anduril #25: output_tokens=12, twice, then queued retries after 2 and 10 minutes that
  succeeded on the same texts). Now the stage re-asks at once with a nudge (`EXTRACT_NUDGE`), on other
  excerpts (the full text when a source fits). Empty again with a normal finish → the stage is degraded
  with diagnostics and the run continues; only a real cut-off (length, content filter, no output)
  queues a retry. Messages give sources, characters sent/available, output and reasoning tokens and
  the finish reason.
- **`/health`**: `worker.queued_now` (runs the worker would start now), `worker.scheduled` (retries
  waiting for a future time) and `worker.next_scheduled_at`. `queue_depth` now equals `queued_now`
  (it counted future retries, e.g. a 24 h energy retry, as queued); `retries_scheduled` is a deprecated
  alias of `scheduled`.
## [0.13.0] - Unreleased (untagged; tag after merge with owner approval)

MINOR: ranking-eligibility change. Component version: **pipeline-v2.9** (follows PR A, 0.11.0 /
pipeline-v2.7, and PR B, 0.12.0 / pipeline-v2.8; prompts, weights and rubrics unchanged). PR C of
`ki-pipeline-issues-2.md`.

### Changed
- **"Energy source couldn't be read" counts only the company's own disclosure.** An unreadable
  source leaves the run unranked only if it is on the company's own domain or a disclosure registry
  (CDP, ResponsibilityReports) and carries an energy-disclosure signal (or is the company's own
  impact/sustainability page or report PDF). Third-party articles never count; SEC-filings indexes,
  press releases, news and blog pages count only when their claim is about energy consumption;
  research's `energy` tag alone is not enough; a `thin` (JavaScript-shell) page needs the signal in its
  URL or title. Under v2.6, Tesla #23 was unranked by four 403 sources; under these rules only
  tesla.com/impact counts. Each listed source now says why it counted (`counted_as`).

## [0.12.0] - Unreleased (untagged; tag after merge with owner approval)

MINOR: fetch change. Component version: **pipeline-v2.8** (follows PR A, which takes 0.11.0 /
pipeline-v2.7; prompts, weights and rubrics unchanged). PR B of `ki-pipeline-issues-2.md`.

### Added
- **Report-PDF discovery**: a report landing page we could read (original or Wayback copy) has its
  disclosure PDF links followed, without model calls: newest year and full edition first, at most 2
  per page and 4 per run (`FETCH_DISCOVER_PER_PAGE`, `FETCH_DISCOVER_MAX`), fetched one at a time
  through the Wayback fallback and the large-PDF reader. Example: tesla.com/impact (403) → its
  2026-06-11 Wayback copy → `2024-extended-version-tesla-impact-report.pdf` (403) → its Wayback copy,
  read by the large-PDF reader. Sources found this way have origin `discovered`.

## [0.11.0] - Unreleased (untagged; tag after merge with owner approval)

MINOR: fetch change. Component version: **pipeline-v2.7** (prompts-v2.4, weights-v1, rubrics-v1
unchanged). Migration **0007** (one additive column). PR A of `ki-pipeline-issues-2.md`.

### Fixed
- **The Wayback fallback never worked in production**: it asked `web.archive.org/wayback/available`,
  which answers HTTP 404, and silently gave up. It now lists the newest HTTP-200 captures with the CDX
  API (falling back to `archive.org/wayback/available` only when CDX fails), so an archived block page
  (tesla.com/impact's newest capture is Akamai's "Access Denied") is never picked; a copy that still
  reads as a bot wall is skipped for the next capture (up to three).

### Added
- `sources.archive_attempt` (migration 0007): what the fallback did for every source that refused us
  (lookup, outcome, note, capture used, captures tried). When no copy is used, the note is appended to
  the source's reason, so "energy source couldn't be read" alerts say whether the archive was tried;
  the admin run page shows a "Wayback: …" badge.
- Archive.org politeness: at most `FETCH_WAYBACK_MAX_REQUESTS` (16) archive requests per run, two at a
  time; a 429, 5xx, timeout or the "Temporarily Offline" page (HTTP 200) stops the fallback for the rest
  of the run. Tests use responses recorded from archive.org (`tests/fixtures/wayback/`).

## [0.10.0] - Unreleased (untagged; tag after merge with owner approval)

MINOR: publishing rules and a new admin action. No component version changes (pipeline-v2.6,
prompts-v2.4, weights-v1, rubrics-v1). Migration **0006** (additive columns).

### Added
- **Retract a run** (admin run page, `POST /admin/runs/{id}/retract`; Hermes `POST /internal/runs/{id}/retract`):
  requires a reason (shown publicly), same-origin protected, audit-logged (who, when, why). A retracted
  run is never current; the most recent remaining run becomes current even if unranked, and the
  dossier shows a dated correction note.

### Changed
- **Newer pipeline wins**: a completed, non-degraded run on a newer pipeline version (and not "energy
  source couldn't be read") replaces the current run even when it is below the ranking thresholds; it
  is shown as Unranked with its reason. Failed runs never replace anything; same-version runs keep the
  existing guard.
- The current run is computed at read time, so both rules apply to existing runs on deploy (e.g.
  Crusoe's pipeline-v2.4 run replaces its v2.2 run that counted 3 GW of planned capacity as operating,
  if that run is not degraded). Admin badges, the withheld alert and the sitemap follow the same rule.

## [0.9.0] - Unreleased (untagged; tag after merge with owner approval)

MINOR: fetch and figure-verification change. Component version: **pipeline-v2.6** (this PR follows
#17, which takes 0.7.0 / pipeline-v2.5, and #18, which takes 0.8.0 / prompts-v2.4; prompts, weights
and rubrics unchanged here). Bug 1, part 3 of the scoring-bug diagnosis.

### Added
- PDFs larger than `FETCH_MAX_BYTES` (30 MB) are read instead of rejected: only the relevant pages
  (outline hits, then the last 40% from the end backwards, pages with energy figures), over HTTP Range
  requests when supported, otherwise streamed to a temporary file on disk. It runs in a separate child
  process with a 256 MB address-space cap, CPU and wall-clock limits, page/byte/disk caps and one
  large PDF at a time, so it cannot exhaust the web process's memory (`LARGE_PDF_*`, see
  docs/API.md). Wayback copies of over-cap PDFs are read the same way.

### Changed
- An energy figure that is one of several unlabelled values under one unit heading (Tesla's key-metrics
  page lists "Energy Consumption (kWh)" with three numbers whose row labels are icons) is rejected as
  ambiguous with the values in the reason; it is never summed unless the source states a total.

## [0.8.0] - Unreleased (untagged; tag after merge with owner approval)

MINOR: research prompt change. Component version: **prompts-v2.4** (this PR follows #16, which takes
prompts-v2.3, and #17, which takes 0.7.0 / pipeline-v2.5; pipeline, weights and rubrics unchanged
here). Bug 1, part 2 of the scoring-bug diagnosis.

### Changed
- Research prefers smaller equivalents of bulky reports: ESG data tables/databooks, KPI or
  performance-data appendices, GRI/SASB/TCFD indexes, CDP climate responses, CSV/XLSX downloads and
  HTML data pages, listed before the full impact report (Tesla's is ~129 MB).
- Candidate sources are re-ordered in code (`app/pipeline/sources.py`) so compact energy data sources
  survive the `JUDGE_MAX_SOURCES` cut and are fetched first; bulky full-report PDFs are still fetched
  when there is room.

## [0.7.0] - Unreleased (untagged; tag after merge with owner approval)

MINOR: ranking/classification change. Component version: **pipeline-v2.5** (this PR follows
0.5.1 / #15 and 0.6.0 / #16, which takes pipeline-v2.4; prompts, weights and rubrics unchanged here).
Bug 1, part 1 of the scoring-bug diagnosis (Tesla shown as "energy undisclosed").

### Changed
- An energy source that was found but couldn't be read (too large, unparseable PDF, no text layer,
  HTTP 401/403/429/5xx, timeout) now makes the run **"energy source couldn't be read"**: not ranked
  (instead of ranked on the remaining 70% with the undisclosed basis), with an admin alert
  (`energy_unreadable`) and one automatic follow-up run 24 h later (`ENERGY_RETRY_*`; a manual re-run
  starts it immediately). Dead links and soft 404s don't count.
- Public label "Energy undisclosed" renamed **"No energy figure found"**; it is used only when every
  energy-related source we found was readable. Leaderboard mini-bar: "no figure".
- `/methodology` and `docs/API.md` describe both cases.

## [0.6.0] - Unreleased (untagged; tag after merge with owner approval)

MINOR: metric-correctness change. Component versions: **pipeline-v2.3 → pipeline-v2.4**,
**prompts-v2.2 → prompts-v2.3** (weights-v1, rubrics-v1 unchanged). Fixes Bug 2 of the scoring-bug
diagnosis (Crusoe "3 GW operating capacity" beside 18.9 MW of measured energy use).

### Fixed
- Future capacity is no longer scored as operating. A `datacenter_capacity_operating` figure becomes
  `datacenter_capacity_planned` (recorded, not scored) when the quote, its own sentence/table row or
  the footnote for a marker in the quote says planned / under development / under construction.
  Footnotes are resolved on both sides of the figure (Crusoe's "* Under development as of March 2026"
  precedes "total capacity* 3 GW" in the extracted PDF text). A footnoted figure whose footnote cannot
  be found is not treated as operating unless the quote itself says so. Neighbouring sentences no
  longer leak into the check.
- Plausibility check: operating capacity that would use more than 10x the company's reported energy
  or electricity consumption (same year ±1) even at 20% utilisation is recorded but not scored, and
  the run is flagged (Crusoe: 3,000 MW vs ~18.9 MW average, 32x).
- Summaries: sentences in the synthesis or opinion rationales that describe planned or unscored
  capacity as operating are removed; the extraction and judge prompts state the rule with the Crusoe
  example.
- Existing runs are not rewritten; the fix applies to the next run of each company.

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
