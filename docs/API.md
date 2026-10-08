# Internal API (Hermes) — v0.2

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
| `WORKER_ENABLED` | `1` | set `0` to disable the in-process worker |
| `WORKER_STALE_S`, `WORKER_MAX_ATTEMPTS` | `180`, `2` | restart recovery: stale runs are re-queued once, then failed |
