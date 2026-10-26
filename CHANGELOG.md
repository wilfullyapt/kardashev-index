# Changelog

All notable changes to the Kardashev Index are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) as described in [docs/RELEASING.md](docs/RELEASING.md).

Component versions (pipeline, prompts, weights, rubrics) are tracked separately in
`app/pipeline/methodology.py` and `app/pipeline/prompts.py`. Each run records the versions it was
scored with. Whenever a component version changes, the entry here says so.

## [Unreleased]

### Changed
- **Public contact:** production `CONTACT_EMAIL` is `contact@kardashevindex.com` (Render). Documented in
  `.env.example`, HANDOFF, and DATA-LICENSE. The local unset-placeholder no longer says "before launch".
  No personal email addresses are present in the current tree. Git history was not rewritten; older
  commits may still mention launch placeholders.


## [1.15.0] — First-party visit counts (no cookies)

### Added
- **Visit counts** (`app/analytics.py`): a middleware records successful HTML page views.
  - Recorded: path (no query string), referrer host only, `utm_source`/`utm_medium`/`utm_campaign`, country from `CF-IPCountry` when the edge sends it, and a user-agent class (human / bot / monitor).
  - Skipped: static files, `/health`, `/admin`, `/internal`, robots/sitemap/favicon, HTMX partials, redirects, errors and non-GET requests.
  - Visitor id: a hash of a daily random salt, the IP and the user agent. The salt lives only in memory and rotates at Pacific midnight. No cookies, no scripts, no raw IP or full user agent stored.
- **Batched writes:** requests never touch the database. Views go into a bounded in-memory buffer (`ANALYTICS_BUFFER_MAX`, default 5000; oldest dropped and counted). A background task writes it every `ANALYTICS_FLUSH_S` (30 s) and once more on shutdown.
- **Retention:** after 3 AM PT, finished days are rolled up into `page_view_daily` (idempotent) and raw rows older than `ANALYTICS_RAW_DAYS` (90) are deleted. Daily totals are kept.
- **Admin board → Traffic** (`GET /admin/analytics`, loaded when the section is opened, cached 60 s): visitors and views per day for 30 days, today vs the 7-day average, top pages, referrers, utm sources and countries, plus the human / bot / monitor share. Humans only by default (`?bots=1` for all).
- `/health` → `analytics`: buffered, written and dropped counts, last flush and error, and `country_header_seen` (whether a `CF-IPCountry` header arrived in the last 24 h).
- Env: `ANALYTICS_ENABLED` (1), `ANALYTICS_FLUSH_S`, `ANALYTICS_BUFFER_MAX`, `ANALYTICS_RAW_DAYS`, `ANALYTICS_EXCLUDE_PATHS`.

### Changed
- **Privacy page:** a new "Visit counts" section says what is counted and how. The page no longer says "no analytics"; it says no *third-party* analytics.

### Database
- Migration **0008** (additive): new tables `page_views` and `page_view_daily`. No existing table changes. Downgrade drops both.

No pipeline, prompt, weights, rubric, scoring or publication changes.

## [1.14.1] — "Run now" for scheduled runs

### Fixed
- A manual re-run of a company whose energy retry was scheduled answered 409 "already active", and the retry still waited for its time. `enqueue_run` cleared the wait before the endpoint could see it.

### Changed
- **Run now:** any manual trigger (admin re-run, Hermes, approve) on a company whose active run is queued with a future `next_attempt_at` now clears the wait.
  - This applies to every kind of scheduled run: deferred or transient retries, `oom`/`crashed`/`killed`/`timeout` requeues, and energy retries.
  - The attempt count and checkpoint are kept.
  - The change is audited as a `run_now` IngestLog event (`run_id`, `prior_next_attempt_at`, the run's trigger and error type, `attempt`, and who asked).
  - The rerun endpoint answers 202 `status: "run_now"` with a message; the admin UI shows a flash and redirects. This replaces the `retry_now` event.
  - The automatic triggers (energy retry, daily sweep) never pull a scheduled run forward.
- **Admin board:** cards and the top queue's scheduled rows show **Run now (scheduled HH:MM PT)** instead of the "Run #N queued…" link. The same button appears on the run page and in the classic table.
- No pipeline, scoring or publication changes.

## [1.14.0] — Memory: each run in its own process, capped downloads and parsing (pipeline-v2.16)

Render's 512 MB instance kept warning and restarted mid-batch (Oct 13, 10:51 AM PT). Its memory graph stepped up with each run and never came back down. The web process also ran every pipeline run, and freed memory stayed with the process.

### Changed
- **Each run executes in a fresh child process** (`python -m app.runproc <run_id>`, `WORKER_SUBPROCESS=1` by default; `0` restores in-process runs). The worker stays in the web process as a supervisor.
  - The supervisor claims the run, keeps the heartbeat going, and waits for the child.
  - The child opens its own database connection and calls the same `execute_run`, so checkpoints, retries, defers and energy retries are unchanged.
  - When the child exits, the run's memory goes back to the OS. Local check: the supervisor stays at about 59 MB after each run, where in-process runs used to ratchet up to ~700 MB.
- **A child that dies is a transient error, re-queued through the normal bounded retry path** (`RUN_MAX_ATTEMPTS`, `RUN_RETRY_DELAYS_MIN`):
  - SIGKILL (the kernel's OOM killer) → `error_type` `oom`.
  - Any other signal → `killed`.
  - A non-zero exit → `crashed`.
  - Running past `WORKER_RUN_TIMEOUT_S` (default 1800) → `timeout`.
- **Shutdown:** the child gets SIGTERM, stops before its next stage, and is re-queued at its checkpoint. After `WORKER_CHILD_TERM_S` (default 40, below `WORKER_DRAIN_S` 45) it is SIGKILLed, and the run is still re-queued without using up an attempt.
- **After every run the supervisor calls `gc.collect()` and `malloc_trim(0)`.** `render.yaml` sets `MALLOC_ARENA_MAX=2`.
- **Downloads:**
  - HTML/text bodies are streamed into a buffer capped at `FETCH_HTML_MAX_BYTES` (8 MB). `FETCH_MAX_BYTES` stays the absolute ceiling.
  - A PDF over `FETCH_PDF_INPROCESS_MAX_BYTES` (5 MB) goes to the large-PDF child (relevant pages only) instead of pypdf in the main process.
  - Raw bodies are dropped as soon as their text is extracted. Report landing pages keep theirs until link discovery has read them.
- **Fetch concurrency:** 3 threads instead of 6 (`FETCH_WORKERS`). One in-process PDF parse at a time instead of 2 (`PDF_PARSE_SLOTS`).
- **Large HTML** (SEC 10-Ks):
  - Cut at `HTML_PARSE_MAX_BYTES` (4 MB) before parsing.
  - Pages over `HTML_LIGHT_PARSE_BYTES` (1 MB) are read with lxml directly instead of BeautifulSoup (about 100× the markup for table-heavy filings), one at a time. These pages skip the embedded-JSON reader; they are documents, not JavaScript shells.
- **Evidence matching:** the quote-matching offset index is a 4-byte C array instead of a list of Python ints (~50 MB per variant for a 1.3M-character 10-K).
- **Large-PDF child memory cap:** 192 MB instead of 256 (`LARGE_PDF_MEMORY_MB`).
- **Header-profile memory** (which hosts need browser headers) now survives the per-run process in a small JSON file (`FETCH_PROFILES_FILE`).

### Added
- `summary.memory` on every run: RSS after each stage, plus the run's peak and its pid/mode.
- `/health` `worker` gains:
  - `mode`, `child_pid`;
  - `rss_mb` and `peak_rss_mb` (web/supervisor process);
  - `last_child` (pid, exit code, peak RSS, seconds) and `last_release` (RSS before/after gc + trim);
  - `last_run.peak_rss_mb`.
- No scoring, weighting, rubric or publication-rule changes. Runs record pipeline-v2.16, because very large pages are now cut before parsing.

## [1.13.0] — Qualitative evidence budget, claims-only follow-up, 0-claims visibility (pipeline-v2.15, prompts-v2.6)

### Changed
- **Extraction budget:** the 3 most qualitative sources (filings excluded) get at least 6,000 chars each (`EXTRACT_QUAL_FLOOR_CHARS`, `EXTRACT_QUAL_SOURCES`). SEC filings and annual reports are capped at 8,000 chars (`EXTRACT_FILING_MAX_CHARS`). Large filers' runs were sending mostly 10-K/ESG tables, leaving opinion categories with no claims.
- **Embedded-JSON reader:** skips site-wide islands (settings/config/nav/cookie…), reads only `__NEXT_DATA__.props.pageProps`, and caps each island at 40k chars.

### Added
- **Claims-only follow-up call** (prompts-v2.6 `CLAIMS_SYSTEM`): at most one per run, when fewer than 3 claims are verified (`EXTRACT_CLAIMS_FOLLOWUP`, default on; `EXTRACT_CLAIMS_MIN`, `EXTRACT_CLAIMS_FOLLOWUP_CHARS` 24k, `EXTRACT_CLAIMS_FOLLOWUP_MAX_TOKENS` 4,000, `EXTRACT_CLAIMS_FOLLOWUP_MAX_USD` 0.05).
  - It is skipped if it could push the run past its cost cap, and it never fails a run.
  - Claims are verified against the fetched text like any other.
- **Visibility:** `summary.claims_verified` on every run (JSON summary, no migration). `/health` gains `runs_7d.zero_claims` and `zero_claims_runs`. The admin board shows a "⚠ 0 claims" card chip and a count in its health block.
- No weight, rubric, scoring or publication-rule changes.

## [1.12.0] — SEC fetch fix, one annual report per run, archive-first for bot-walled hosts (pipeline-v2.14)

### Fixed
- **sec.gov 403s ("Undeclared Automated Tool"):** the energy retry's `browser_first` strategy and the large-PDF reader sent a browser User-Agent to sec.gov. Every SEC request now carries `SEC_EDGAR_USER_AGENT`, whatever headers the caller passed.
- SEC URLs are never retried with browser headers and never looked up in the Wayback Machine.
- SEC full-submission `.txt` files (whole filing plus exhibits, often 10–20 MB) are never fetched; the primary document carries the report.

### Changed
- At most one annual report (10-K / 20-F / 40-F) per run. Preference: the edgar stage's candidate, then sec.gov, then the newest other copy; IR mirrors are skipped when a better copy exists. Skipped candidates are listed in the fetch stage detail.
- New `FETCH_ARCHIVE_FIRST_HOSTS` (default `tesla.com,www.tesla.com,ir.tesla.com`; empty = off): hosts whose bot manager refuses every client are looked up in the Wayback Machine first, and fetched live only if no usable copy exists.
- `/health` `config.sec_edgar_user_agent_format_ok`, plus a startup warning when `SEC_EDGAR_USER_AGENT` isn't `Name contact@domain`.
- No scoring, weighting, rubric or publication-rule changes. Runs record pipeline-v2.14.

## [1.11.0] — Pipeline versions on every run surface (admin and public)

### Added
- Admin: shared chip `adm.ver(run)` ("pipeline v2.13"; the tooltip lists prompts / rubrics / weights). A run that never started shows "not started", since versions are recorded at start. Shown on board cards (last run, active run, and the current run when it differs), queue rows, the Classic companies table (current + last), the runs list (with prompts · rubrics · weights) and history compare (with prompts · rubrics · weights).
- Public: each leaderboard row has a small pipeline-version chip (e.g. "v2.13"; the tooltip lists prompts / rubrics / weights). The public run history table (company and past-run pages) now includes the prompts version, in the same order as the company page's method line: pipeline · prompts · rubrics · weights.

### Changed
- Admin phone fixes: larger, multi-line card meta; Retract is a 44px button; history dates on two lines with a visible "swipe sideways" hint; queue rows put status and version on their own line.
- Display only: no scoring, publication, data or DB changes.

## [1.10.0] — Admin run history compare view

### Added
- `/admin/companies/{id}/history` is now a side-by-side compare of a company's past runs (newest 50): pipeline version, date (PT), coverage and measured coverage, confidence, Index, ranked (with the not-ranked reason), source count, cost, and status (published/withheld/degraded/retracted/failed). The run the public page shows is marked current. On phones the table scrolls sideways with the run column pinned. It replaces the 1.8.0 redirect to the runs list.

## [1.9.0] — Admin board: live queue, health, spend, add company

### Added
- Top of `/admin`: a live queue (running, queued, scheduled retries with PT times); system health reused from the `/health` observability block (failures by type, worst sites, archive.org usage today); and spend today and this month (PT) from recorded run costs. Runs with no recorded cost are counted, and the total shows "unknown" when nothing was recorded.
- `GET /admin/board/top` partial: HTMX refreshes it every 10 s only while something is running, queued or scheduled, and polling stops when idle.
- Add-company form (`POST /admin/companies/add`, name and/or ticker → Run). An exact name or ticker match re-runs the existing company. The ticker is stored as a hint; identity resolution still decides the entity (pin the identity to force it).

## [1.8.0] — Phone-first admin board

### Added
- `/admin` is a phone-first board: stacked cards with 44px tap targets and collapsible sections. Ranked companies are sorted by Index; unranked ones fall into four groups (energy unreadable → degraded/failed → low confidence → low coverage; first match wins, documented in docs/API.md).
- Cards show name, score or reason, last run (PT) and cost, Re-run with a cost-estimate confirm, Retract/Un-retract, Details and History.
- `POST /admin/runs/{id}/unretract`: clears a retraction, recomputes the current run under the existing rules, and logs `run_unretracted`. It removes the public correction note.
- `GET /admin/companies/{id}/history` (redirects to the filtered runs list for now).

### Changed
- The previous dashboard moved to `/admin/classic`, and every feature is still there. No scoring or publication-rule changes.

## [1.7.0] — Resilience: fetch fallbacks, honest retries, observability

One release combining what were PRs #28–#34 (follows 1.0.1 / #27). **Component change: pipeline-v2.9 →
pipeline-v2.13** (prompts-v2.5, weights-v1, rubrics-v1 unchanged). Scoring math and ranking
thresholds are unchanged. The pipeline steps below (v2.10 to v2.13) are recorded for history; runs only ever
carry v2.9 or v2.13. Under "newer pipeline wins", a *clean* v2.13 run can replace a v2.9 current
run even if unranked (degraded / energy-unreadable runs never do).

### Alternative sources: SEC annual reports and ResponsibilityReports.com

**Component change: pipeline-v2.12 → pipeline-v2.13** (prompts-v2.5, weights-v1, rubrics-v1
unchanged). Scoring math and ranking thresholds are unchanged; runs can now draw verified figures
from two more kinds of source. **Methodology note:** a registry copy can be an older report than the
company's current one (e.g. a 2022 report when the newest is locked); its figures carry their own
period like any other.

#### Added
- SEC annual report as a source (`app/pipeline/edgar.py` `latest_annual_report`, runner
  `_add_annual_report`): the edgar stage reads `data.sec.gov/submissions/CIK….json` and adds the
  newest original 10-K / 20-F / 40-F primary document as a candidate (`origin: filing`, primary,
  fetched under `SEC_EDGAR_USER_AGENT` and SEC's rate limit). Annual reports often state
  electricity use, capacity and capex plans, and sec.gov doesn't bot-wall a declared client.
- Registry fallback (`app/pipeline/registries.py`): when no energy-tagged source was read, or the
  company's own energy disclosure couldn't be read, the company is looked up on
  ResponsibilityReports.com and its newest report PDF is fetched (`origin: registry`; the "most
  recent" report unless locked, else the newest archived one). Lookup failures never fail a run.
- CDP is **not** looked up automatically (its response pages are built in the browser and mostly
  need an account); cdp.net pages found by research are still fetched and count as a registry.
- `EDGAR_ANNUAL_REPORT` and `FETCH_REGISTRY_FALLBACK` (both default `1`).

#### Cost
- No paid infrastructure. Up to 3 extra HTTP requests (SEC submissions + document, registry search +
  page + PDF) per run; a 10-K adds text competing for the same extraction budget
  (`EXTRACT_MAX_CHARS`), so xAI cost per run is unchanged.

### Escalating energy retries with a different strategy each time

**Component change: pipeline-v2.11 → pipeline-v2.12** (prompts-v2.5, weights-v1, rubrics-v1
unchanged). Scoring math and ranking rules are unchanged; energy retries fetch differently.

#### Changed
- Energy-unreadable retries (`app/pipeline/runner.py` `schedule_energy_retry`): up to
  `ENERGY_RETRY_MAX` = **3** (was 1) consecutive retries, waiting `ENERGY_RETRY_DELAYS_HOURS` =
  **6, 24, 72 h** (was a fixed 24 h). Each retry carries the previous run's unreadable URLs (tried
  again even if research doesn't list them) and a strategy: 1st `archive_first` (Wayback copy
  before the origin), 2nd `browser_first` (browser headers first), 3rd `standard`. The plan is kept
  in the retry's checkpoint and logged on `energy_retry_scheduled`.
- The public reason names the retry ("retry scheduled for Oct 13, 15:00 PT (automatic retry 1)");
  the methodology page describes the escalation from the live settings.

#### Added
- `ENERGY_RETRY_DELAYS_HOURS` (default `6,24,72`). `ENERGY_RETRY_DELAY_HOURS` still works: when set
  and the plural isn't, it applies to every retry. Set `ENERGY_RETRY_MAX=1` to restore one retry.

#### Cost
- Each retry is a normal paid run (~$0.35, `SWEEP_EST_RUN_USD`): at most ~$1.05 per company per
  energy-unreadable episode (was ~$0.35). Retries do not count against the daily sweep cap.

### Defer instead of publish when sources fail only transiently

**Component change: pipeline-v2.10 → pipeline-v2.11** (prompts-v2.5, weights-v1, rubrics-v1
unchanged). Scoring math and ranking thresholds are unchanged. What changes is *when* a run
finishes: a run whose missing data failed only for passing reasons no longer completes (and
possibly publishes) on an early attempt.

#### Changed
- End of the fetch stage (`app/pipeline/runner.py` `_defer_if_only_transient`): if the company's
  energy disclosure couldn't be read **only** because of a rate limit, outage or timeout (429/5xx,
  timeout, or a refused original whose Wayback fallback was rate-limited, down or capped), or no
  source at all was readable and every failure was of that kind, the run raises
  `fetch_transient` (transient). It is re-queued through the existing retry path
  (`RUN_RETRY_DELAYS_MIN` 2/10/60 min, `RUN_MAX_ATTEMPTS` 4) and re-fetches on the next attempt.
  Nothing is published meanwhile. The final attempt completes exactly as before (e.g.
  "energy couldn't be read"), so runs never loop.
- Permanent answers (a block the archive has no copy of, 404, thin page, unreadable PDF) are not
  deferred.

#### Added
- `FETCH_DEFER_TRANSIENT` (default `1`; `0` restores the previous behaviour).

#### Cost
- A deferred attempt has already paid for resolve + research (~$0.05–0.10 of the per-run cap);
  extract and judge only run once fetching succeeds or on the final attempt.

### Per-host User-Agent strategy

No scoring change (pipeline-v2.10, prompts-v2.5, weights-v1, rubrics-v1 unchanged): the same pages
are judged by the same rules; fewer of them are refused.

#### Changed
- Fetching (`app/pipeline/fetch.py` `HttpFetcher`): non-SEC hosts are now asked first as an honest,
  identifiable crawler (`KardashevIndexBot/1.0 (+https://www.kardashevindex.com/about)`, minimal
  headers). On 401/403/406 the request is repeated once with the browser headers used before; the
  profile that got through is remembered per host (`HostProfiles`, in process, 7-day expiry) and
  tried first next time. SEC hosts keep `SEC_EDGAR_USER_AGENT`; a caller's own User-Agent wins.
  Each answer records the profile used (`FetchResult.profile`).

#### Added
- `FETCH_UA_STRATEGY` (`honest_first` default; `browser` restores the pre-1.4.0 behaviour) and
  `FETCH_BOT_USER_AGENT` (override the bot string). Documented in docs/API.md and HANDOFF.md.

#### Note
- A host that blocks bots costs one extra request the first time per process (restarts forget).
  The large-PDF reader keeps its browser headers.

### Read JavaScript-built pages' embedded data

**Component change: pipeline-v2.9 → pipeline-v2.10** (prompts-v2.5, weights-v1, rubrics-v1
unchanged). Scoring math and ranking rules are unchanged; pages that used to read as "thin" can now
supply verified figures. Note: under "newer pipeline wins", a *clean* v2.10 run replaces an older
v2.9 current run even if unranked (degraded / energy-unreadable runs never do).

#### Added
- Embedded page data (`app/pipeline/fetch.py` `embedded_json_texts`): when an HTML page's visible
  text is under 2,000 characters, prose from its Next.js `__NEXT_DATA__`, JSON-LD and other
  `application/json` data islands is appended to the page text (deduplicated; URLs, ids and code
  skipped; capped at 200,000 characters). JS-built sites such as Anduril's stop reading as "thin".
  Quotes are verified against this text like any other; no headless browser and no new
  infrastructure.

### /health observability

No scoring change (pipeline-v2.9, prompts-v2.5, weights-v1, rubrics-v1 unchanged). `/health` change
is additive (new `observability` object); existing fields and status codes are unchanged.

#### Added
- `/health` → `observability` (`app/observability.py`, cached `OBS_CACHE_S` = 60 s so Render's
  frequent polling stays cheap):
  - `runs_7d`: runs finished in the last `OBS_WINDOW_DAYS` (7) by outcome (`succeeded`,
    `degraded`, `energy_unreadable`, `withheld`, `failed:<error_type>`);
  - `hosts_7d`: the 10 hosts whose sources failed most, with counts by status (`dead:403`, `thin`,
    `error`…) and Wayback outcome (`archive:rate_limited`…);
  - `retry_claims_without_queued_run`: companies whose public reason says "retry scheduled" while
    nothing is queued (should be empty);
  - `archive`: real archive.org requests used today vs `FETCH_WAYBACK_DAILY_MAX`.
- New env vars (optional): `OBS_CACHE_S` (60), `OBS_WINDOW_DAYS` (7).

### archive.org pacing, backoff and budgets

No scoring change (pipeline-v2.9, prompts-v2.5, weights-v1, rubrics-v1 unchanged): the same
sources are accepted or rejected by the same rules; the Wayback fallback just gives up less easily.

#### Changed
- Wayback fallback (`app/pipeline/fetch.py` `Wayback`): archive.org requests are paced
  (`FETCH_WAYBACK_MIN_INTERVAL_S`, default 1.5 s, one at a time, shared by all runs and threads);
  a 429/5xx/outage is retried with backoff, honouring `Retry-After` up to
  `FETCH_WAYBACK_RETRY_AFTER_CAP_S` (120 s; `FETCH_WAYBACK_RETRIES`, default 2); if archive.org
  still refuses, the run pauses (`Retry-After` or `FETCH_WAYBACK_PAUSE_S`, 60 s) and probes once
  more (`FETCH_WAYBACK_RESUMES`, default 1) before the breaker opens for the rest of the run.
- Every real archive.org request now counts toward `FETCH_WAYBACK_MAX_REQUESTS` (16): the HTTP
  client's own 429 retries are turned off for archive requests, so bursts are no longer hidden.

#### Added
- `FETCH_WAYBACK_DAILY_MAX` (default 400): real archive.org requests per UTC day across all runs in
  the process; `archive_attempt.outcome` is `capped` with a "daily cap" note when it is reached.
- New env vars (all optional, safe defaults): `FETCH_WAYBACK_MIN_INTERVAL_S`,
  `FETCH_WAYBACK_RETRIES`, `FETCH_WAYBACK_RETRY_AFTER_CAP_S`, `FETCH_WAYBACK_RESUMES`,
  `FETCH_WAYBACK_PAUSE_S`, `FETCH_WAYBACK_DAILY_MAX` (documented in docs/API.md and HANDOFF.md).

#### Note
- A run that meets a rate limit can now spend up to a few minutes in the fetch stage (pauses);
  the worker heartbeat runs separately, so this is not mistaken for a stalled run.

## [1.0.1] — Honest "retry scheduled" wording

No scoring change (pipeline-v2.9, prompts-v2.5, weights-v1, rubrics-v1 unchanged).

### Fixed
- "Energy couldn't be read" reasons no longer always end in "retry scheduled". The suffix is now
  written after the run commits and states what actually happened: "retry scheduled for <date,
  time> PT" when an energy retry was queued, "no automatic retry queued (retry limit reached); the
  daily sweep re-checks it later" when `ENERGY_RETRY_MAX` was reached, or "automatic retries are
  off". The run summary records `energy_retry_run_id`.
- Runs stored before 1.0.1 (e.g. Tesla, TSMC) carried "; retry scheduled" even when no retry was
  queued; that legacy suffix is dropped at display time (`app/eligibility.public_reason`).

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
