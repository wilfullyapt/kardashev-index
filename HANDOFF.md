# Handoff: running the Kardashev Index

For a new owner or operator taking over the product and the business. As of **v1.0.0** (the public
launch baseline). Everything here was checked against the code at that version. Items marked
**(unknown, owner to fill)** are not visible in the repository.

Never put secret values in this file, the repository, issues or PRs.

---

## 1. What the product is

A public leaderboard that ranks companies by how hard they push civilization up the Kardashev scale.
Live at <https://kardashev-index.onrender.com> (no custom domain yet).

- **65% of every score is measured**: reported energy throughput (30%), data-center / compute
  capacity (20%) and growth (15%), converted to physical units and scored in code.
- **35% is opinion**, labelled as such: a model scores frontier acceleration (15%), builder velocity
  (12%) and policy stance / permission to build (8%) against written rubrics, citing only quotes
  that were verified in documents the app fetched itself.
- Companies are suggested publicly (`/suggest`), approved by an admin (or by Hermes, the internal
  agent, via `/internal/*`), then measured by the pipeline. Every run, source, quote and metric
  is stored; history is never overwritten.
- Public pages: `/` (leaderboard), `/companies/{id}` (dossier with history),
  `/companies/{id}/runs/{ref}`, `/methodology`, `/about`, `/privacy`, `/suggest`, `/robots.txt`,
  `/sitemap.xml`, `/health`.

## 2. Architecture

| Part | What it is |
|---|---|
| Web | FastAPI (`app/main.py`), Jinja2 templates (`templates/`), HTMX 1.9.12 vendored (`static/vendor/`), hand-written CSS/SVG (`static/`). Single uvicorn process. |
| Database | Postgres on Render (SQLite for local tests). SQLAlchemy models in `app/models.py`, Alembic migrations in `alembic/versions/` (0001–0007, all applied by the build). |
| Worker | In-process background worker (`app/worker.py`) started with the web app (`WORKER_ENABLED`). Runs are queued in Postgres (`judgment_runs`), picked up in order, heartbeated, checkpointed per stage, and resumed after a restart or deploy. Transient failures retry automatically (`RUN_RETRY_DELAYS_MIN`, `RUN_MAX_ATTEMPTS`). |
| LLM | xAI API over httpx (`app/pipeline/llm.py`): Responses API with web search for resolve/research, Chat Completions for extract/judge. Model `grok-4.3` (`XAI_MODEL`). Paid. |
| SEC | EDGAR XBRL company facts for capex/revenue (`app/pipeline/edgar.py`), needs `SEC_EDGAR_USER_AGENT`. Free, rate limited. |

**Pipeline stages** (`app/pipeline/runner.py`, `STAGES`):
`resolve → research → edgar → fetch → extract → compute → judge → aggregate`.

- **resolve**: official name, domain, ticker, CIK (reused for `IDENTITY_TTL_DAYS`, or pinned by an admin).
- **research**: candidate primary sources via web search (capped searches and sources).
- **edgar**: XBRL facts for US filers.
- **fetch**: every source is fetched by the app itself. SSRF-guarded; dead links and soft 404s are
  rejected; the text snapshot and sha256 are stored. PDFs over the size cap go to the large-PDF reader,
  which reads only the relevant pages in a resource-capped child process. Pages that refuse the app
  fall back to the Wayback Machine (CDX lookup, block-page skip, rate-limit breaker; every attempt is
  recorded on the source). Report PDFs linked from readable report pages are followed.
- **extract**: figures and claims. A quote is kept only if it is verbatim in the fetched text and the
  number and unit match. An empty answer gets one immediate nudged re-ask.
- **compute**: measured categories in code (`app/pipeline/measures.py`).
- **judge**: opinion categories from verified evidence only.
- **aggregate**: fixed weights, coverage, confidence and the ranking gates (`RANK_*`).

**Methodology versions** (recorded on every run): `PIPELINE_VERSION` (pipeline-v2.9),
`WEIGHTS_VERSION` (weights-v1), `RUBRIC_VERSION` (rubrics-v1) in `app/pipeline/methodology.py`, and
`PROMPT_VERSION` (prompts-v2.5) in `app/pipeline/prompts.py`. Weights, anchors and rubrics live in
`methodology.py`; the public explanation is `/methodology`.

**Publication and the current run** (`app/publication.py`, computed at read time):

- A ranked run always publishes.
- An unranked run never replaces a ranked one. It replaces an unranked current run only if it scored
  at least as much weight, and it never replaces real legacy v0 scores.
- **Newer pipeline wins**: a completed, non-degraded run on a newer pipeline version (and not
  "energy source couldn't be read") replaces the current run even when unranked. Failed runs never
  replace anything.
- **Retracted** runs are never current. The next most recent remaining run becomes current, and the
  dossier shows a dated correction note.
- **"Energy source couldn't be read"**: if no energy figure was verified and the company's own energy
  disclosure was found but unreadable, the run is not ranked, an alert is raised, and one retry is
  scheduled (`ENERGY_RETRY_*`). Rules are in `app/pipeline/disclosure.py`.

## 3. Repository layout

```
app/                FastAPI app
  main.py           routes (public, admin, /internal), startup, /health
  models.py, db.py  SQLAlchemy models, engine/session
  worker.py         in-process worker, retries, daily sweep
  runs.py           enqueue/rerun, admin views, run filters
  publication.py    current-run rules (read time), retraction
  alerts.py         alert collection and webhook
  security.py       session, CSRF same-origin check, security headers, HSTS
  seo.py            canonical host redirect, robots.txt, sitemap.xml
  disclosure.py     contact address, model name, measured share (display only)
  pipeline/         runner (stages), llm, fetch, largepdf, discover, disclosure, edgar,
                    evidence, semantics, measures, aggregate, sources, prompts, methodology, config
alembic/            migrations (versions/0001–0007)
templates/, static/ pages, CSS, images, vendored htmx
tests/              pytest suite (fake LLM and HTTP; no network, no spend), fixtures/
scripts/            check_version_bump.py (CI version-check)
docs/API.md         internal API, pipeline details, all configuration
docs/RELEASING.md   branching, versioning, deploys, rollback
render.yaml         Render Blueprint (web service + Postgres)
Dockerfile, docker-compose.yml   local / self-hosted stack
CHANGELOG.md, LICENSE (MIT), DATA-LICENSE.md (CC BY-NC 4.0), NOTICE.md
```

## 4. Local development

```bash
cp .env.example .env          # fill in ADMIN_*, SECRET_KEY; XAI_API_KEY only if you want paid runs
docker compose up --build     # Postgres 17 + app on http://localhost:8088
```

docker-compose sets `SWEEP_ENABLED=0` by default, so there are no automatic paid re-runs locally.
Without `XAI_API_KEY`, runs fail with `config_error` (no spend). To create an admin password hash:
`python -c "from passlib.hash import bcrypt; print(bcrypt.hash('yourpass'))"`.

Tests and lint (Python 3.12.15):

```bash
pip install -r requirements-dev.txt     # or: pip install -r requirements.txt pytest
pytest -q                                # SQLite; fake LLM/HTTP; no network, no API spend
TEST_DATABASE_URL=postgresql://... pytest -q   # same suite on Postgres (CI job test-postgres)
ruff check .
python scripts/check_version_bump.py origin/main
```

## 5. Release process

Details: [docs/RELEASING.md](docs/RELEASING.md). Summary of what GitHub enforces (rulesets, verified):

- **`main` branch ruleset**: no direct pushes (PR required), squash merge only, linear history, no
  force-push, no deletion. Required status checks: **`lint`, `test`, `test-postgres`,
  `version-check`**, with the branch up to date with `main` (strict). 0 approving reviews required.
  No bypass actors.
- **`release tags v*` ruleset**: `v*` tags cannot be deleted, moved or force-updated. Tag right the
  first time.
- **Versioning**: SemVer in `app/version.py` plus a `CHANGELOG.md` section, in the PR that makes the
  change. `version-check` fails if a component version changes or a migration is added without at
  least a MINOR bump. From 1.0.0 on, breaking changes to `/internal/*`, the `/health` contract, or
  non-backward-compatible migrations are MAJOR.
- **Tags** are cut from `main` after merge, with owner approval: `git tag -a vX.Y.Z -m "vX.Y.Z" <sha>`.
  Tags v0.1.0–v0.14.0 exist. v1.0.0 is planned for the commit that ships this file.

## 6. Deploys

- **Render Blueprint** (`render.yaml`): one Python web service `kardashev-index` and one Postgres
  database `kardashev-index-db` (`plan: basic-256mb`, region `oregon`).
- Build: `pip install --only-binary :all: -r requirements.txt && alembic upgrade head`.
  Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- **Python 3.12.15**, pinned by `PYTHON_VERSION` in `render.yaml` and `.python-version`; CI and the
  Dockerfile use the same version.
- **CI-gated**: `autoDeployTrigger: checksPass`. Render deploys a `main` commit only after its checks
  pass. Never use `[skip ci]` on `main`; use `[skip render]` to skip a deploy deliberately.
- **Health check**: `healthCheckPath: /health`. Traffic switches only when it returns 2xx. It returns
  200 when the database is reachable and 503 when it isn't; worker state is informational only.
  `maxShutdownDelaySeconds: 60` lets the worker checkpoint a running stage (`WORKER_DRAIN_S=45`).
- **Verify**: `curl -s https://kardashev-index.onrender.com/health`. `code_version` must equal the
  merge commit and `version` must equal `app/version.py`.
- **Rollback**: use Render dashboard → service → Deploys → **Rollback** to the last good deploy, then
  fix forward with a revert PR. Avoid "Deploy a specific commit", which turns auto-deploy off. Check
  *Settings → Auto-Deploy* afterwards. Before any migration that rewrites or deletes data, take a
  `pg_dump`.

## 7. Environment variables

**Where set**:
- *Blueprint*: fixed value in `render.yaml`.
- *Render secret*: listed in `render.yaml` with `sync: false`; the value is entered in the Render
  dashboard.
- *Render generated / injected*: Render creates or injects it.
- *Default*: not in `render.yaml`; the code default applies unless someone added it in the Render
  dashboard. The dashboard was not inspected for this document.

The full list with comments is in `.env.example` and `docs/API.md#configuration-environment`.

### Core, security, public host

| Variable | Purpose | Required | Default | Where set |
|---|---|---|---|---|
| `DATABASE_URL` | Postgres connection string | yes (prod) | `sqlite:///./test.db` | Render injected (`fromDatabase`) |
| `SECRET_KEY` | signs admin session cookies; production refuses to start without it | yes (prod) | random per process (dev only) | Render generated (`generateValue`) |
| `ADMIN_EMAIL` | admin login email | yes | none | Render secret |
| `ADMIN_PASSWORD_HASH` | bcrypt hash of the admin password | yes | none | Render secret |
| `HERMES_API_KEY` | key for `/internal/*` (header `X-Hermes-Key` or `?hermes_key=`) | yes, for Hermes | none | Render secret |
| `SESSION_MAX_AGE_S` | admin session lifetime | no | `43200` (12 h) | Default |
| `SESSION_COOKIE_SECURE` | Secure flag on the session cookie | no | on in production | Default |
| `HSTS_MAX_AGE` | `Strict-Transport-Security` max-age (HTTPS only) | no | `86400` (1 day) | Default (raise at launch, §11) |
| `HSTS_INCLUDE_SUBDOMAINS` | add `includeSubDomains` | no | off | Default |
| `PUBLIC_BASE_URL` | canonical origin: 301 from every other host (`/health`, `/internal/*` exempt); canonical/og/sitemap URLs | no (launch) | unset (no redirect) | not set yet (launch, §11) |
| `CONTACT_EMAIL` | corrections / privacy / commercial-licensing contact on `/about`, `/privacy`, footer | no (launch) | placeholder text | not set yet (launch, §11) |
| `APP_ENV` | `production` forces production mode outside Render | no | unset | not needed on Render |
| `PYTHON_VERSION` | Render runtime pin | yes (Render) | — | Blueprint (`3.12.15`) |
| `RENDER`, `RENDER_INSTANCE_ID`, `RENDER_GIT_COMMIT`, `PORT` | production detection, worker id, deployed commit, listen port | — | — | Render injected |
| `GIT_COMMIT` / `SOURCE_VERSION` | deployed commit outside Render | no | — | other hosts only |

### xAI (paid)

| Variable | Purpose | Required | Default | Where set |
|---|---|---|---|---|
| `XAI_API_KEY` | xAI API key; without it every run fails with `config_error` and admin shows an alert | yes, for runs | none | Render secret |
| `GROK_API_KEY` | alias of `XAI_API_KEY` | no | none | Render secret (in Blueprint) |
| `XAI_MODEL` | model name | no | `grok-4.3` | Default |
| `XAI_BASE_URL` | API base | no | `https://api.x.ai/v1` | Default |
| `XAI_TIMEOUT_S` / `XAI_SEARCH_TIMEOUT_S` | chat / search call timeouts | no | `120` / `240` | Default |
| `XAI_MAX_RETRIES` / `XAI_BACKOFF_MAX_S` | in-call retries for 408/409/429/5xx/timeouts; max wait | no | `3` / `60` | Default |

### Budget and research limits (per run)

| Variable | Purpose | Default |
|---|---|---|
| `JUDGE_MAX_COST_USD` | hard per-run budget, checked before every paid call | `0.40` |
| `JUDGE_RESERVE_USD` | budget kept back for the judge | `0.04` |
| `JUDGE_RESOLVE_MAX_SEARCHES` / `JUDGE_RESEARCH_MAX_SEARCHES` | web-search tool calls | `3` / `8` |
| `JUDGE_MAX_SOURCES` | sources fetched per run | `12` |
| `JUDGE_RESOLVE_MAX_TOKENS` / `JUDGE_RESEARCH_MAX_TOKENS` / `JUDGE_EXTRACT_MAX_TOKENS` / `JUDGE_JUDGE_MAX_TOKENS` | output token caps per stage | `8000` / `16000` / `16000` / `8000` |
| `JUDGE_MAX_ATTEMPTS` | LLM repair attempts per call | `2` |

All optional; Default (not in `render.yaml`).

### Fetching, extraction, large PDFs, Wayback, discovery

| Variable | Purpose | Default |
|---|---|---|
| `SEC_EDGAR_USER_AGENT` | contact string sent to sec.gov (SEC requires it); without it EDGAR is skipped and admin shows an alert. **Set** (Render secret; `/health` reports `sec_edgar_user_agent: true`) | none |
| `FETCH_TIMEOUT_S` / `FETCH_MAX_BYTES` / `FETCH_RETRIES` | per-request timeout, download cap, retries | `20` / `30000000` / `2` |
| `FETCH_WAYBACK` / `FETCH_WAYBACK_MAX_REQUESTS` | Wayback fallback on/off; archive.org requests per run | `1` / `16` |
| `FETCH_DISCOVER_PER_PAGE` / `FETCH_DISCOVER_MAX` | report PDFs followed per page / per run (`0` = off) | `2` / `4` |
| `SNAPSHOT_MAX_CHARS` | fetched text stored per source | `1000000` |
| `EXTRACT_MAX_CHARS` / `EXTRACT_PER_SOURCE_CHARS` | text sent to extraction in total / per source | `64000` / `20000` |
| `LARGE_PDF_ENABLED` | read over-cap PDFs in a capped child process | `1` |
| `LARGE_PDF_MEMORY_MB`, `LARGE_PDF_CPU_S`, `LARGE_PDF_TIMEOUT_S`, `LARGE_PDF_DEADLINE_S`, `LARGE_PDF_RANGE_BUDGET_MB`, `LARGE_PDF_MAX_DOWNLOAD_MB`, `LARGE_PDF_SCAN_PAGES`, `LARGE_PDF_QUIET_PAGES`, `LARGE_PDF_KEEP_PAGES`, `LARGE_PDF_MAX_CHARS`, `LARGE_PDF_STREAM_MB` | child-process limits (memory, CPU, wall clock, bytes, pages, characters) protecting the 512 MB instance | `256`, `90`, `150`, `120`, `48`, `200`, `120`, `40`, `30`, `400000`, `16` |
| `LARGE_PDF_LIMITS` | internal: passed from the parent process to the child. **Do not set.** | — |

### Identity and ranking

| Variable | Purpose | Default |
|---|---|---|
| `IDENTITY_TTL_DAYS` / `IDENTITY_MIN_CONFIDENCE` | reuse a resolved identity | `90` / `0.75` |
| `ENERGY_COUNT_GENERATION` | own generation counts as energy throughput | `1` |
| `RANK_MIN_COVERAGE` / `RANK_MIN_MEASURED` | ranking gates | `0.6` / `0.3` |
| `RANK_MIN_MEASURED_SHARE` / `RANK_MIN_CONFIDENCE` | quality gates (strictly above) | `0.4` / `0.2` |
| `RANK_ENERGY_UNDISCLOSED` / `RANK_ENERGY_UNDISCLOSED_MIN_SOURCES` | rank on the remaining 70% when no energy figure is disclosed | `1` / `3` |
| `ENERGY_RETRY_ENABLED` / `ENERGY_RETRY_DELAY_HOURS` / `ENERGY_RETRY_MAX` | energy-unreadable follow-up run (paid) | `1` / `24` / `1` |

### Worker, retries, daily sweep, alerts

| Variable | Purpose | Default |
|---|---|---|
| `WORKER_ENABLED` | run the in-process worker (`0` for previews or a second instance) | `1` |
| `WORKER_POLL_S` / `WORKER_HEARTBEAT_S` / `WORKER_STALE_S` / `WORKER_DRAIN_S` | poll interval, heartbeat, stale-run requeue, shutdown drain (keep below 60) | `15` / `20` / `180` / `45` |
| `RUN_AUTO_RETRY` / `RUN_MAX_ATTEMPTS` / `RUN_RETRY_DELAYS_MIN` | automatic retries of transient failures | `1` / `4` / `2,10,60` |
| `SWEEP_ENABLED` / `SWEEP_HOUR_UTC` / `SWEEP_MIN_AGE_DAYS` | daily re-run of withheld / unranked companies (paid) | `1` / `10` (03:00 PDT) / `7` |
| `SWEEP_DAILY_COST_USD` / `SWEEP_EST_RUN_USD` | daily sweep spend cap; per-run estimate | `2.0` / `0.35` |
| `ALERT_WEBHOOK_URL` | Slack-compatible JSON webhook for failed / degraded / stuck runs. In Blueprint as a Render secret but **not set** (`/health` reports `alert_webhook: false`) | none |

### Local / test only

`TEST_DATABASE_URL` (run pytest on Postgres), `POSTGRES_PASSWORD` (docker-compose database password).

## 8. Operations

- **Admin** (`/admin/login`, one account from `ADMIN_EMAIL` / `ADMIN_PASSWORD_HASH`; login is
  throttled; sessions last 12 h):
  - `/admin`: companies, suggestions, alert banner.
  - `/admin/runs`: filters by status, including `withheld`.
  - `/admin/runs/{id}`: versions, commit, stage timings, tokens, cost, sources with Wayback badges,
    extracted evidence with verification results.
- **Re-run**: the "Re-run" buttons in admin, or `POST /internal/rerun-judgment/{company_id}` (admin
  session or Hermes key). Returns 409 if a run is already queued or running. Re-running a company with
  a scheduled retry starts it now. Published data changes only if the new run succeeds.
- **Retract**: `POST /admin/runs/{id}/retract` from the run page (reason of 10–500 characters, shown
  publicly; same-origin check; audit-logged), or `POST /internal/runs/{id}/retract`. **There is no
  un-retract.**
- **Identity pin**: `POST /admin/companies/{id}/identity` pins a corrected name, domain, ticker or CIK,
  which every later run reuses. `action=clear` makes the next run resolve again.
- **Alerts**:
  - The admin banner shows failed, retrying, degraded, withheld, stuck and energy-unreadable runs, plus
    config warnings (missing `XAI_API_KEY` or `SEC_EDGAR_USER_AGENT`).
  - The same data is available at `GET /internal/alerts`.
  - The optional webhook is `ALERT_WEBHOOK_URL` (currently unset).
- **/internal endpoints for Hermes** (header `X-Hermes-Key`; an admin session also works):
  - `alerts`, `health`, `stats`, `recent-judgments`, `logs`, `hermes-activity`;
  - `approve/{suggestion_id}`, `deny/{suggestion_id}`, `test-suggestion`;
  - `runs`, `runs/{id}`, `runs/{id}/retract`, `rerun-judgment/{company_id}`.
  - See [docs/API.md](docs/API.md).
- **Daily sweep**: after 10:00 UTC, once a day, it re-queues withheld or not-ranked current runs older
  than 7 days, within `SWEEP_DAILY_COST_USD` ($2.00/day by default, estimated at $0.35 per run).
- **Energy retry**: one automatic follow-up run 24 h after an energy-unreadable run (`ENERGY_RETRY_MAX=1`).
- **Cost per run**: about **$0.08–0.10 observed**. Hard cap **$0.40** per run (`JUDGE_MAX_COST_USD`),
  checked before every paid call. Every stage's tokens and cost are stored on the run.
- **xAI spend limit**: set a monthly spending limit in the xAI console as the backstop to the per-run
  and sweep caps. Current value: **(unknown, owner to fill)**.
- **Uptime monitor**: an external monitor should poll `/health` (200 means up; `status: degraded`
  means the worker has stalled). Provider and account: **(unknown, owner to fill)**.
- **/health** fields: `version`, `pipeline_version`, `prompt_version`, `code_version`, and
  `worker.{alive, queued_now, scheduled, next_scheduled_at, queue_depth, last_run}`.

## 9. Monthly costs

| Item | Plan | Amount |
|---|---|---|
| Render web service | paid instance, 0.5 CPU / 512 MB (owner-reported; the plan is not pinned in `render.yaml`) | **(unknown, owner to fill)**; see the Render billing page |
| Render Postgres | `basic-256mb` (from `render.yaml`) | **(unknown, owner to fill)** |
| xAI API | usage-based: about $0.08–0.10 per run, plus the daily sweep (capped at $2/day) and energy retries | variable, bounded by the xAI spend limit |
| Domain | not purchased yet | **(unknown)** |
| SEC EDGAR, Wayback Machine | free public services | $0 |

## 10. Data licensing

- **Code**: MIT ([LICENSE](LICENSE)).
- **Scores, data and methodology text**: **CC BY-NC 4.0** from v0.15.0 onward
  ([DATA-LICENSE.md](DATA-LICENSE.md)).
- **Earlier grant**: content published by **v0.5.0–v0.14.0** was CC BY 4.0. Those grants are
  irrevocable and stand. v0.4.x and earlier declared no data license.
- **Commercial use, bulk or automated access, and resale** need a separate written license. Contact is
  `CONTACT_EMAIL` (currently the placeholder; set it at launch).
- Facts and figures quoted from company sources are not covered. Third-party assets are listed in
  [NOTICE.md](NOTICE.md).

## 11. Launch checklist (in order)

1. **`CONTACT_EMAIL`**: set it in the Render dashboard. It replaces the placeholder on `/about`,
   `/privacy` and the footer, and is the commercial-licensing contact.
2. **Domain**: buy it, add it as a custom domain on the Render service, configure DNS, and wait for
   Render's TLS certificate.
3. **`PUBLIC_BASE_URL`**: set it to `https://<domain>` once the domain serves HTTPS. Every other host,
   including `*.onrender.com`, then gets a 301; canonical and sitemap URLs use it. Exempt from the
   redirect: `/health` and `/internal/*`, so the health check and Hermes keep working. Confirm the
   uptime monitor URL.
4. **Sitemap submission**: submit `https://<domain>/sitemap.xml` in Google Search Console and Bing
   Webmaster Tools (`robots.txt` already points to the sitemap).
5. **Raise HSTS**: after a few days without problems on the domain, set `HSTS_MAX_AGE=31536000`. Add
   `HSTS_INCLUDE_SUBDOMAINS=1` only if every subdomain serves HTTPS.

## 12. Known limitations and follow-ups

- **No un-retract.** Reversing a retraction needs a new run or a database edit.
- **The admin "withheld" filter uses the stored `published` flag**, while the public current run is
  computed at read time (retraction, newer-pipeline-wins). The two can disagree for older runs until
  the stored flag is re-synced.
- **JS-rendered sites read as "thin"**: pages that build their content in JavaScript (e.g.
  anduril.com) yield little text, so those companies get less evidence. There is no headless browser.
- **Tesla's energy figure depends on the Wayback Machine.** tesla.com answers 403 to the fetcher, so
  energy comes from archived copies of tesla.com/impact and its linked 2024 report (FY2024 data). If
  archive.org is down or rate-limits, the run degrades, the failure is recorded, and the run can end
  as energy-unreadable.
- **Repository access of the AI engineer**: the AI engineer's GitHub CLI session uses an OAuth token
  with `repo`, `workflow`, `read:org` and `gist` scopes on the owner account. Those scopes are broad
  enough to change settings and rulesets, even though its working rules forbid that. **Recommendation**:
  revoke it and issue a **fine-grained personal access token** limited to this repository, with
  Contents read/write, Pull requests read/write, Actions read and Metadata read, and no administration.
  Alternatively, use a separate machine user. Rotate it on handoff.

## 13. Accounts and access to transfer

| Account | What it controls | Notes |
|---|---|---|
| GitHub repository `wilfullyapt/kardashev-index` | code, CI, rulesets, tags | transfer the repo or add the new owner as admin; review rulesets and tokens (§12) |
| Render | web service, Postgres, env vars and secrets, deploys, billing | transfer the workspace or the services; rotate `SECRET_KEY` (logs everyone out), `ADMIN_PASSWORD_HASH`, `HERMES_API_KEY` |
| xAI | API key, billing, spend limit | issue a new `XAI_API_KEY` under the new owner, then revoke the old one |
| Domain registrar | domain and DNS | **(not purchased yet)** |
| SEC EDGAR contact email | the address in `SEC_EDGAR_USER_AGENT` | update it to an address the new owner reads |
| Uptime monitor | `/health` checks and notifications | **(provider unknown, owner to fill)** |
| Hermes (internal agent) | uses `HERMES_API_KEY` for `/internal/*` | rotate the key on Render and in Hermes together |
| Alert webhook (if set up) | `ALERT_WEBHOOK_URL` | currently unset |

## 14. Contacts

| Role | Name | Contact |
|---|---|---|
| Current owner | **(owner to fill)** | **(owner to fill)** |
| New owner / operator | **(owner to fill)** | **(owner to fill)** |
| Corrections / licensing | — | `CONTACT_EMAIL` (not set yet) |
| Render billing | **(owner to fill)** | **(owner to fill)** |
| xAI account | **(owner to fill)** | **(owner to fill)** |
