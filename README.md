# Kardashev Index

Public read-only rankings of entities by alignment to **Effective Accelerationism** (e/acc).

- Climb the Kardashev scale
- Unrestricted technocapital & AI progress
- Builder culture over deceleration

**Status**: Pre-deployment. Single commit ready for Render.

## Features
- Public leaderboard with overall score + per-category columns
- Server-side sort, category filter, and search
- Company scorecards with full historical tracking
- Read-only public API (`/api/companies`)
- CSV export (`/export.csv`)
- Admin-gated suggestions queue + approval workflow with Grok (xAI) judging
- Rate limiting + improved evidence prompts

## Stack
- Python + FastAPI
- Postgres (Render)
- Jinja2 + Tailwind
- SQLAlchemy + Alembic

## Deployment
Deploy via Render using `render.yaml`.

MIT License (once public)
