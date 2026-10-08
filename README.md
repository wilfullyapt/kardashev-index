# Kardashev Index

A public leaderboard that ranks companies by how hard they are pushing civilization up the
Kardashev scale — in the spirit of **Effective Accelerationism** (e/acc).

**65% of every score is measured** (reported energy, data-center capacity and growth, converted
to watts and scored in code). **35% is opinion**, labelled as such: a model scores three
categories against written rubrics, citing only quotes we have verified in documents we fetched
ourselves. Full details: [`/methodology`](https://kardashev-index.onrender.com/methodology).

## How a score is made (pipeline-v2.1)

1. **Resolve** the entity (official name, site, ticker, CIK) — xAI model + web search.
2. **Research** primary sources (sustainability/ESG reports, filings, announcements) — web search, capped.
3. **SEC EDGAR** XBRL company facts for capex and revenue (US filers; needs `SEC_EDGAR_USER_AGENT`).
4. **Fetch** every source ourselves: SSRF-guarded, dead links and soft-404s rejected (including a
   random-path probe), sha256 + text snapshot stored.
5. **Extract** figures and claims from the most relevant parts of each document; a quote is kept only
   if it is present in the fetched text (spacing, hyphenation and footnote markers tolerated, words and
   digits not), the number is in it and the unit is nearby.
6. **Compute** energy (TWh/yr → watts → Kardashev-equivalent), compute (MW) and growth in code,
   on fixed log-scale anchors.
7. **Judge** frontier acceleration, builder velocity and permission-to-build against anchored
   rubrics, from verified quotes and figures only ("insufficient evidence" is a valid answer).
8. **Aggregate** with fixed weights in code (no model-produced overall). Missing data lowers
   confidence; too little data means "not ranked yet". A run below the ranking thresholds never
   replaces better published data (a ranked run, or legacy v0 scores); it is kept and noted instead.

Runs are queued in Postgres and executed by an in-process worker, so approving a suggestion
returns immediately. Every run, stage (timings, tokens, cost), source, quote and metric is
stored; history is never overwritten. Each run has a hard budget (`JUDGE_MAX_COST_USD`, default
$0.40).

Each company page keeps its full public history: every published run is labelled relative to the
current one (N, N-1, …) and has a read-only dossier, a trend line and per-category deltas. Admins get a
runs list and a per-run detail view with versions, code commit, timings, cost and errors (see `docs/API.md`).

Code: `app/pipeline/` (`methodology.py` holds weights, anchors and rubrics; `measures.py` the
math), `app/worker.py`, `app/runs.py`. Internal API: [`docs/API.md`](docs/API.md).

## Stack

- Python + FastAPI, Jinja2, a hand-rolled e/acc design system (`static/css/kardashev.css`,
  original procedural SVG art in `static/img/`)
- SQLAlchemy + Alembic, Postgres on Render
- xAI API over httpx (Responses API with web search; Chat Completions)

## Development

```
pip install -r requirements.txt pytest
pytest -q            # uses fake LLM/HTTP; no network, no API spend
ruff check .
```

`TEST_DATABASE_URL=postgresql://...` runs the suite against Postgres.

## Deployment

Render (`render.yaml`): build runs `alembic upgrade head`; single uvicorn worker. Set
`XAI_API_KEY`, `HERMES_API_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD_HASH` and
`SEC_EDGAR_USER_AGENT`. Other knobs are listed in [`docs/API.md`](docs/API.md#configuration-environment).

MIT License (once public)
