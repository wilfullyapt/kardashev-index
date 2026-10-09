# Internal API (Hermes) — v0.3.0

All `/internal/*` endpoints accept either an admin session cookie or the Hermes key
(`X-Hermes-Key` header, or `?hermes_key=`). Unauthenticated requests get `401`.

## What changed in v0.2 (pipeline-v2.0)

Judging no longer happens inside the HTTP request. Approve and re-run **queue a run and
return immediately with `202`**; an in-process worker executes the run (resolve → research →
EDGAR → fetch → extract → compute → judge → aggregate) and records every stage.

| Endpoint | Before | Now |
|---|---|---|
| `POST /internal/approve/{suggestion_id}` | `200`, blocked until the LLM finished; `judgment: "success"/"failed"` | `202`; `judgment: "queued"` or `"already_queued"` / `"already_running"`; adds `run_id`, `run_url` |
| `POST /internal/rerun-judgment/{company_id}` | `200`, blocked; overwrote scores | `202 {status: "rerun_queued", run_id, run_url}`; `409 {status: "already_active", run_id, run_status}` if a run is queued/running; `404` unknown company |
| `GET /internal/runs/{run_id}` | — | new: full run record incl. stages and category scores |
| `GET /internal/runs?company_id=&status=&limit=` | — | new: run list (newest first, max 100) |
| `GET /internal/stats` | | adds `runs: {status: count}` |
| `GET /internal/recent-judgments` | legacy scores | unchanged `recent_judgments` (legacy v0 scores, no longer written) plus new `recent_runs` |
| approve / deny / re-run from an admin HTML form | JSON | `303` redirect back to `/admin` |

History is never overwritten: each run is a new row. The public page and leaderboard show the
company's *current* run, which only changes when a newer run succeeds (and, if the current run
is ranked, only when the new run is ranked too). Failed runs never publish.

## Approve

```
POST /internal/approve/42
202 {
  "status": "approved", "suggestion_id": 42, "company_id": 7, "canonical_name": "nvidia",
  "approved_by": "hermes", "judgment": "queued", "run_id": 15, "run_url": "/internal/runs/15"
}
```

`404` if the suggestion is not pending (approving twice is safe). Approving a name that maps to
an existing company queues a run for that company (or reports the run that is already active).

## Re-run

```
POST /internal/rerun-judgment/2
202 {"status": "rerun_queued", "company_id": 2, "canonical_name": "nvidia", "rerun_by": "hermes",
     "run_id": 16, "run_url": "/internal/runs/16"}
409 {"status": "already_active", "run_status": "running", "run_id": 15, ...}
```

## Run record

`GET /internal/runs/16`

```
{
  "run_id": 16, "company_id": 2, "status": "running", "current_stage": "fetch",
  "trigger": "rerun", "triggered_by": "hermes", "attempt": 1,
  "queued_at": "...", "started_at": "...", "finished_at": null, "duration_ms": null,
  "model": "grok-4.3", "model_returned": "...", "input_tokens": 0, "output_tokens": 0,
  "reasoning_tokens": 0, "tool_calls": 0, "cost_usd": 0.0, "budget_usd": 0.4,
  "index_score": null, "measured_score": null, "judged_score": null,
  "k_equivalent": null, "avg_power_w": null, "confidence": null, "coverage": null,
  "ranked": null, "published": null, "error_type": null, "error": null,
  "pipeline_version": "pipeline-v2.0", "prompt_version": "prompts-v2.0",
  "rubric_version": "rubrics-v1", "weights_version": "weights-v1", "prompt_hash": "…",
  "summary": {...},
  "stages": [{"stage": "resolve", "seq": 1, "attempt": 1, "status": "succeeded",
              "started_at": "...", "finished_at": "...", "duration_ms": 8123, "model": "grok-4.3",
              "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "tool_calls": 2, "cost_usd": 0.03,
              "error": null, "detail": {...}}, ...],
  "categories": [{"category": "energy_throughput", "family": "measured", "score": 2.1,
                  "confidence": 0.9, "weight": 0.3, "rationale": "...", "evidence_ids": [3],
                  "insufficient": false}, ...],
  "sources": {"ok": 6, "dead": 1, "soft_404": 1}
}
```

Run `status`: `queued` → `running` → `succeeded` | `failed`.
`error_type` on failure: `budget_exceeded`, `api_error` (xAI call failed after retries),
`validation_error` (model output invalid after a repair attempt), `config_error` (e.g. no API
key), `interrupted` (worker died twice mid-run), or `internal_error`.
`published: false` with `summary.publish_note` means the run succeeded but did not replace the
current (ranked) run because it was not itself ranked.

## Admin and public views (v0.2.1)

Admin pages need the admin session. Browsers that aren't logged in are redirected to
`/admin/login?next=…`; non-HTML clients get `401`. None of these pages are linked from the public site,
and all of them send `noindex`.

| Page | What it shows |
|---|---|
| `GET /admin` | pending suggestions, latest runs (live), every company with its current/last run, run count and a **Re-run** button, activity log |
| `GET /admin/runs?company_id=&status=&page=` | every run, newest first, 25 per page; filters by company and status; refreshes itself while a run is queued/running |
| `GET /admin/runs/{id}` | one run: queued/started/finished (Pacific) and duration, queue wait, attempt/worker, pipeline/prompt/rubric/weights versions, prompt bundle hash, **code version**, model requested/returned, cost vs budget, tokens, searches, per-stage timings/tokens/cost/errors/detail JSON, category scores, every source (including rejected ones and why); re-run button; polls every 3 s while active |

Re-run from an admin form asks for confirmation (paid API). It then redirects to the new run's page with a flash
message. If a run is already active, the form redirects to that run instead of a 409. The JSON API keeps the 409.
`judgment_runs.code_version` (migration 0003) records `RENDER_GIT_COMMIT` (or `GIT_COMMIT`) at run start.

Public history (published runs only; failed or unpublished runs, costs, tokens and prompts are never shown):

| URL | |
|---|---|
| `GET /companies/{id}` | current dossier + run history table (N, N-1, …), Index/K sparklines, per-category Δ vs the previous run |
| `GET /companies/{id}/runs/{n}` | read-only dossier of the n-th published run (stable ordinal, 1 = first). The current run redirects to `/companies/{id}` |
| `GET /companies/{id}?run=N-2` | resolves a relative label to the stable URL (302) |
| `GET /companies/{id}/runs/v0` | legacy v0 single-prompt scores, labelled superseded (only when real legacy scores exist) |

## Publishing guard and extraction fixes (pipeline-v2.1, prompts-v2.1)

**Publishing guard** (`app/publication.py`). A successful run below the ranking thresholds is recorded
in full but does not become the company's published (current) run when better data exists: a ranked run,
an unranked run that scored more of the weight, or real (non-placeholder) legacy v0 scores. Such runs have
`published: false` and a `summary.publish_note`; admins see them with a **withheld** tag
(`/admin/runs?status=withheld`). The public page keeps showing the best data and adds a dated note
("A later run … found too little verified data … not published"); withheld runs get no N number and no
dossier URL. Migration **0004** replays this rule over existing runs (data-only, idempotent, no deletes).

**Current run at read time; retraction; newer pipeline wins** (0.10.0, `publication.replay`). The
public (current) run is computed when a page is served, from the company's succeeded runs, so these
rules apply to existing data on deploy without a data migration (`companies.current_run_id` is kept as
a cache, re-synced after a run or a retraction):
- **Newer pipeline wins.** A completed run on a newer pipeline version (`pipeline-v2.10 > v2.9`) than
  the current run replaces it even when it is below the ranking thresholds; it is shown as Unranked
  with its reason and the older run stays in the history. Only a clean run qualifies: not `degraded`
  and not "energy source couldn't be read". Failed runs never replace anything. Same-version runs keep
  the guard above. Stored `published` flags are otherwise respected.
- **Retraction.** `POST /admin/runs/{id}/retract` (admin session, same-origin check, form field
  `reason`, 10–500 characters) or `POST /internal/runs/{id}/retract` (admin session or Hermes key,
  JSON `{"reason": "…"}`; 422 on a missing/short reason or a run that is not completed/already
  retracted). A retracted run is never current and is removed from the public history. Later runs are
  re-decided without it; if none qualifies, the most recent remaining completed run becomes current even
  if unranked. The dossier shows "Correction (date): … retracted — reason". Stored in
  `judgment_runs.retracted_at / retracted_by / retraction_reason` (migration **0006**, additive) and
  audit-logged in `ingest_logs` (`action = "run_retracted"`, admin, reason, current run before/after).
  There is no un-retract action (a new run, or a database edit, restores data).

**Extraction/verification** (`app/pipeline/evidence.py`):
- quotes are matched on the sequence of letters and digits, so line breaks, hyphenation splits
  ("trillion- parameter"), Unicode spaces, curly quotes, glued/split words, footnote markers
  ("emissions.1") and an elided middle ("…") no longer cause rejections; different words or digits still
  do. The stored quote is the matching span copied from the fetched source;
- table rows ("Energy consumption 1,053,479 815,864 593,953") yield one number per cell; the value must
  appear in the matched source span *and* in the model's quote;
- units accept spellings and scale words ("megawatt hours", "MWh/yr", "thousand MWh" → GWh, "$ millions");
- long documents are split into ~1 kB chunks; ~60% of each source's budget goes to energy/capacity
  tables and figures, the rest to qualitative evidence; boilerplate ranks last; the overall budget is
  shared by relevance (`EXTRACT_MAX_CHARS` 64 000, `EXTRACT_PER_SOURCE_CHARS` 20 000); identical
  documents (same sha256) are read once (`status: duplicate`);
- the extract stage detail records what was sent per source (`input`), rejections by reason, and how
  quotes matched (`match_methods`);
- the judge runs whenever any verified evidence exists (quotes, and verified figures as context), so
  opinion categories are scored even when measured data is too thin to rank.

## Reliability and metric correctness (pipeline-v2.2, prompts-v2.2)

- **Metric definitions** are enforced in the prompt and in code (`app/pipeline/semantics.py`):
  energy *consumed* (and own *generation*) feeds energy throughput; `energy_storage_deployed` and
  `energy_sold` are recorded but never scored (a storage quote filed as consumption is reclassified).
  Capex must be the cash-flow "purchases of property and equipment" line or company-reported capex
  for a completed period in a primary source: bonds/notes, funding rounds, deal or order sizes,
  planned/forecast spend and headlines are rejected with a recorded reason. Plausibility bounds,
  parenthesized numbers `( 11,339 )`, dot leaders `....` and table "(in millions)" headers are handled.
- **Future capacity** (pipeline-v2.4, prompts-v2.3): `datacenter_capacity_operating` is reclassified
  to `datacenter_capacity_planned` (recorded, not scored) when the quote, its own sentence/table row, or
  the footnote for a marker in the quote (`*`, `†`, `¹`…, searched ±2,500 characters because PDF text
  places footnotes anywhere on the page) says planned / under development / under construction; a
  footnoted figure whose footnote cannot be found is not treated as operating unless the quote says
  so. Operating capacity whose energy floor (capacity × 1 year × 20%) exceeds 10× the reported
  energy or electricity consumption (same year ±1) is recorded but not scored, with a flag. Sentences
  in the synthesis or opinion rationales that describe planned or unscored capacity as operating are
  removed (`semantics.strip_future_as_current`).
- **Large PDFs** (pipeline-v2.6): a PDF over `FETCH_MAX_BYTES` is no longer simply rejected. A
  separate, resource-capped process reads only its relevant pages (outline hits, then the last 40% from
  the end backwards, keeping pages with energy figures), over HTTP Range requests when the server
  supports them, otherwise via a download streamed to a temporary file. The source's reason records
  how it was read ("large PDF (182 MB, 210 pages) read via HTTP Range: 1 relevant pages kept…").
  Limits (`LARGE_PDF_*`, below) protect the 512 MB web process; on failure the source stays rejected
  with the reason appended.
- **Unlabelled values** (pipeline-v2.6): an energy figure that is one of several bare numbers under a
  single unit heading with no row labels (and no "total" in the quote) is rejected as ambiguous, with
  the values listed in the reason; it is never summed. Values that line up with year columns
  ("2024 2023 612,000 489,600") are treated as labelled.
- **Quotes** are verified against the full fetched text (stored up to `SNAPSHOT_MAX_CHARS`), and a quote
  attributed to the wrong fetched source is matched against the others.
- **Source preference** (prompts-v2.4, `app/pipeline/sources.py`): research asks for compact data
  equivalents first (ESG data tables/databooks, KPI or performance-data appendices, GRI/SASB/TCFD
  indexes, CDP responses, CSV/XLSX, HTML data pages) and lists them before a full impact report, which
  can be a 100+ MB PDF. Candidates are then re-ordered in code (stable): research picks before bare
  citations, compact energy data sources boosted, bulky full-report PDFs slightly lowered, so the
  compact ones survive the `JUDGE_MAX_SOURCES` cut and are fetched and excerpted first.
- **EDGAR**: XBRL company facts supply revenue and capex (`source_url` = the companyfacts API URL,
  quote carries the accession number). Every sec.gov request sends `SEC_EDGAR_USER_AGENT` and is
  rate-limited to 8 req/s. Without the user agent the stage is skipped and the admin pages show a
  config alert.
- **Identity** is cross-checked against SEC `company_tickers.json` (status `confirmed`, `name_match`,
  `mismatch`, `not_listed`, `unchecked` in the resolve stage detail). A confident resolution is saved on
  the company and reused for `IDENTITY_TTL_DAYS`; admins can pin or clear it
  (`POST /admin/companies/{id}/identity`, form on /admin). A pinned identity is always used.
- **Checkpoints**: each completed stage is saved in `run.checkpoint`; a retried or interrupted run
  resumes after the last completed stage (compute and aggregate always re-run) without repeating paid
  calls. Errors are classified `transient` / `permanent` (`run.error_class`). Transient failures are
  retried automatically after +2, +10, +60 min (`RUN_RETRY_DELAYS_MIN`) up to `RUN_MAX_ATTEMPTS`
  attempts in total; `next_attempt_at` is shown on the run. Re-running a run that is waiting for a
  retry starts it now.
- **Fetch**: browser-like headers, retries for 429/5xx/timeouts honoring `Retry-After`, and a Wayback
  Machine fallback for 401/403/404/410/451 (stored as `archive_url` / `archive_timestamp` and labelled
  "archived copy" publicly). PDFs are parsed page-capped with a second parser fallback.
- **Wayback lookup** (pipeline-v2.7): the newest three HTTP-200 captures are listed with the CDX API
  (`web.archive.org/cdx/search/cdx?...&filter=statuscode:200`), so an archived bot wall (e.g. Akamai's
  403 page captured as-is) is never chosen; `archive.org/wayback/available` is used only when CDX itself
  fails (the old `web.archive.org/wayback/available` endpoint answers 404). A copy that reads as a block
  page ("Access Denied", Cloudflare/captcha walls) is skipped for the next capture. At most
  `FETCH_WAYBACK_MAX_REQUESTS` archive requests per run (default 16), two at a time; a 429, 5xx,
  timeout or the "Temporarily Offline" page (served with HTTP 200) stops the fallback for the rest of
  the run. Every attempt is stored on the source as `archive_attempt` (`lookup`, `outcome`: used /
  not_found / unusable / rate_limited / unavailable / capped, `note`, `used`, `tried`) and, when no
  copy was used, appended to the source's reason (shown on the admin run page).
- **Graceful degradation**: the budget cap never discards gathered work: remaining gathering stages
  stop, a reserve is kept for the judge, and the run completes with `run.degraded` listing what was
  skipped. The judge output is validated leniently (bad ids dropped, missing categories marked
  insufficient); an empty extraction is retried once on a different chunk selection.
- **Energy undisclosed rule**: with no verified energy figure after at least
  `RANK_ENERGY_UNDISCLOSED_MIN_SOURCES` usable sources, a company can be ranked on the remaining 70% of
  the weight at the same 60% threshold, flagged `energy_undisclosed`, with confidence × 0.8.
- **Quality gate** (pipeline-v2.3): after either coverage rule passes, a run is ranked only if the
  measured share of the scored weight (`measured_coverage / coverage`) is > `RANK_MIN_MEASURED_SHARE`
  (0.40) and `confidence` (0–1; shown as 0–100%) is > `RANK_MIN_CONFIDENCE` (0.20). The gate is also
  recomputed at display time for stored runs (`app/eligibility.py`); `ranked` in API responses is that
  effective value and `ranked_at_run` is the verdict stored when the run finished.
- **Unranked index** (0.5.1): every run stores `index_score` (the weighted mean of whatever was
  scored), but for an unranked run it is a raw number over too little data and is not comparable.
  Public pages never show it: run history and the header read "Unranked", deltas compare ranked runs
  only, and the Index sparkline plots ranked runs only. The Hermes JSON endpoints (`/internal/runs*`,
  `/internal/recent-judgments`) keep the raw `index_score` for diagnostics alongside `ranked`; clients
  must not display it as an Index when `ranked` is false.
- **Energy source couldn't be read** (pipeline-v2.5): if energy throughput has no verified figure and
  an energy-related source (research tagged it `energy`, or its URL/title looks like an impact,
  sustainability, ESG, CDP, emissions or data-table document) exists but could not be read — status
  `error` (over `FETCH_MAX_BYTES`, unparseable PDF), `thin` (no text layer) or `dead` with HTTP
  401/403/406/429/451/5xx or a timeout — the run is **not ranked** (`rank_basis: energy_unreadable`,
  `summary.energy_unreadable` lists the sources), it never takes the "no energy figure found"
  (formerly "energy undisclosed") basis, an `energy_unreadable` alert is raised (admin banner,
  `/internal/alerts`, webhook) and a follow-up run is queued (`trigger: energy_retry`,
  `next_attempt_at` = now + `ENERGY_RETRY_DELAY_HOURS`). A manual re-run starts that queued retry
  immediately. Dead links (404/410) and soft 404s don't count.
- **Daily sweep** (default on): once a day after `SWEEP_HOUR_UTC`, withheld or not-ranked current runs
  older than `SWEEP_MIN_AGE_DAYS` are re-queued, capped at `SWEEP_DAILY_COST_USD` per day (estimated
  `SWEEP_EST_RUN_USD` per run). Logged as `auto_sweep`.
- **Alerts**: `GET /internal/alerts?days=7` (Hermes key) returns failed / retrying / degraded /
  withheld / stuck runs and config warnings; the same list is a banner on the admin pages. If
  `ALERT_WEBHOOK_URL` is set, failures, degradations and stuck runs are POSTed to it as JSON
  (`{"event": ..., "run_id": ..., "company": ..., "text": ...}`; Slack-compatible `text`).
- **`GET /health`** reports `worker` (`alive`, `last_tick`, `queue_depth`, `current_run_id`, `retries_scheduled`,
  `last_run` outcome), versions and config flags (`edgar`, `xai`, `webhook`). Public; no secrets.

## Unchanged

`/internal/health`, `/internal/logs` (new actions: `judgment_run`, `judge_error`),
`/internal/deny/{id}`, `/internal/test-suggestion`, `/internal/hermes-activity`.

## Configuration (environment)

| Variable | Default | Purpose |
|---|---|---|
| `XAI_API_KEY` (or `GROK_API_KEY`) | — | required for runs; without it runs fail with `config_error` |
| `XAI_MODEL` | `grok-4.3` | model for every stage |
| `JUDGE_MAX_COST_USD` | `0.40` | hard per-run budget; checked before every call |
| `SEC_EDGAR_USER_AGENT` | unset | contact string for SEC EDGAR, e.g. `Kardashev Index you@example.com`; EDGAR stage is skipped if unset |
| `JUDGE_RESEARCH_MAX_SEARCHES` / `JUDGE_RESOLVE_MAX_SEARCHES` | `8` / `3` | web-search tool-call caps |
| `JUDGE_MAX_SOURCES` | `12` | candidate sources fetched per run |
| `RANK_MIN_COVERAGE` / `RANK_MIN_MEASURED` | `0.6` / `0.3` | weight that must be scored to be ranked |
| `RANK_MIN_MEASURED_SHARE` / `RANK_MIN_CONFIDENCE` | `0.4` / `0.2` | ranked only if measured share of scored weight and confidence are strictly above these |
| `ENERGY_RETRY_ENABLED` / `ENERGY_RETRY_DELAY_HOURS` / `ENERGY_RETRY_MAX` | `1` / `24` / `1` | when an energy source was found but couldn't be read: schedule a follow-up run this many hours later, at most this many in a row per company (each is a normal paid run) |
| `EXTRACT_MAX_CHARS` / `EXTRACT_PER_SOURCE_CHARS` | `64000` / `20000` | document text sent to the extract stage (total / per source) |
| `SNAPSHOT_MAX_CHARS` | `1000000` | fetched text stored per source (quotes are verified against it on resume) |
| `XAI_MAX_RETRIES` / `XAI_BACKOFF_MAX_S` | `3` / `60` | in-call retries for 408/409/429/5xx and timeouts (honors `Retry-After`) |
| `FETCH_RETRIES` / `FETCH_WAYBACK` / `FETCH_MAX_BYTES` | `2` / `1` / `30000000` | fetch retries, Wayback fallback, max download size |
| `FETCH_WAYBACK_MAX_REQUESTS` | `16` | archive.org requests (lookups + copies) per run |
| `LARGE_PDF_ENABLED` | `1` | read over-cap PDFs in a capped child process (0 = reject them as before) |
| `LARGE_PDF_MEMORY_MB` / `LARGE_PDF_CPU_S` / `LARGE_PDF_TIMEOUT_S` / `LARGE_PDF_DEADLINE_S` | `256` / `90` / `150` / `120` | child address-space cap, CPU seconds, wall-clock kill, internal stop-and-return deadline |
| `LARGE_PDF_RANGE_BUDGET_MB` / `LARGE_PDF_MAX_DOWNLOAD_MB` | `48` / `200` | bytes transferred via HTTP Range; largest streamed download (needs 2× free disk) |
| `LARGE_PDF_SCAN_PAGES` / `LARGE_PDF_QUIET_PAGES` / `LARGE_PDF_KEEP_PAGES` / `LARGE_PDF_MAX_CHARS` / `LARGE_PDF_STREAM_MB` | `120` / `40` / `30` / `400000` / `16` | pages examined, stop after this many pages without a hit, pages kept, text kept, per-stream decompression cap |
| `JUDGE_RESOLVE_MAX_TOKENS` / `JUDGE_RESEARCH_MAX_TOKENS` / `JUDGE_EXTRACT_MAX_TOKENS` / `JUDGE_JUDGE_MAX_TOKENS` | `8000` / `16000` / `16000` / `8000` | output token limits |
| `JUDGE_RESERVE_USD` | `0.04` | budget kept for the judge when gathering stages hit the cap |
| `IDENTITY_TTL_DAYS` / `IDENTITY_MIN_CONFIDENCE` | `90` / `0.75` | reuse of a saved auto identity |
| `ENERGY_COUNT_GENERATION` | `1` | count own generation as energy throughput |
| `RANK_ENERGY_UNDISCLOSED` / `RANK_ENERGY_UNDISCLOSED_MIN_SOURCES` | `1` / `3` | energy-undisclosed ranking rule |
| `RUN_AUTO_RETRY` / `RUN_MAX_ATTEMPTS` / `RUN_RETRY_DELAYS_MIN` | `1` / `4` / `2,10,60` | automatic retries of transient failures (replaces `WORKER_MAX_ATTEMPTS`) |
| `SWEEP_ENABLED` / `SWEEP_HOUR_UTC` / `SWEEP_MIN_AGE_DAYS` / `SWEEP_DAILY_COST_USD` / `SWEEP_EST_RUN_USD` | `1` / `10` / `7` / `2.0` / `0.35` | daily re-run sweep of withheld/not-ranked runs |
| `ALERT_WEBHOOK_URL` | unset | optional JSON webhook for failures/degradations/stuck runs |
| `WORKER_ENABLED` | `1` | set `0` to disable the in-process worker |
| `WORKER_STALE_S` / `WORKER_DRAIN_S` | `180` / `45` | stale-heartbeat recovery (resumes from checkpoint); shutdown drain time |
