## What & why
<!-- Plain-language summary of the change and the reason for it. -->

## Type
- [ ] feat  - [ ] fix  - [ ] chore / ci / docs / test

## Versioning (see docs/RELEASING.md)
- [ ] Pipeline / prompt / weights / rubric version changed → bumped `app/version.py` (≥ MINOR) and added a `CHANGELOG.md` section
- [ ] New Alembic migration → bumped (≥ MINOR), migration is expand/contract-safe, `pg_dump` plan if it touches data
- [ ] User-visible change → `CHANGELOG.md` updated (under the new version or `[Unreleased]`)
- [ ] None of the above

## Deploy
- [ ] Changes the running app (deploys on merge)
- [ ] No runtime impact → squash title may include `[skip render]`
- [ ] Needs new/changed env vars on Render (list them; `.env.example` updated)

## Checks
- [ ] `ruff check .` and `pytest` pass locally
- [ ] No secrets, seed data, `*.db` or `__pycache__` committed
- [ ] Paid LLM calls not triggered by tests
