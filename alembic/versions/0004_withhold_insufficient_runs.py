"""Publishing guard, applied retroactively to existing runs.

A run below the ranking thresholds must not be the public (current) run when better data exists — a ranked run, an unranked run that scored more of
the weight, or real legacy v0 scores (see app/publication.py, mirrored here so this migration
does not depend on application code).

Data-only and idempotent: replays each company's successful runs in completion order, then
updates judgment_runs.published / summary.publish_note and companies.current_run_id where the
result differs. Runs, scores and evidence are never deleted. Downgrade is a no-op.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08
"""
from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

PLACEHOLDER_MODEL = "stub"
WITHHELD_LEGACY = "kept the legacy v0 scores public: this run was below the ranking thresholds"

companies = sa.table("companies", sa.column("id", sa.Integer), sa.column("current_run_id", sa.Integer))
runs = sa.table("judgment_runs", sa.column("id", sa.Integer), sa.column("company_id", sa.Integer),
                sa.column("status", sa.String), sa.column("finished_at", sa.DateTime),
                sa.column("ranked", sa.Boolean), sa.column("coverage", sa.Float),
                sa.column("published", sa.Boolean), sa.column("summary", sa.JSON))
scores = sa.table("scores", sa.column("company_id", sa.Integer), sa.column("model", sa.String),
                  sa.column("justification", sa.Text))


def decide(ranked, coverage, prev, has_legacy):
    if ranked:
        return True, None
    if prev is not None:
        if prev["ranked"]:
            return False, f"kept run {prev['id']} published: it was ranked and this run was not"
        if (prev["coverage"] or 0) > (coverage or 0):
            return False, (f"kept run {prev['id']} published: it scored more of the weight "
                           f"({prev['coverage'] or 0:.0%} vs {coverage or 0:.0%})")
        return True, None
    if has_legacy:
        return False, WITHHELD_LEGACY
    return True, None


def upgrade() -> None:
    bind = op.get_bind()
    legacy = {row.company_id for row in bind.execute(sa.select(scores.c.company_id, scores.c.model,
                                                                scores.c.justification))
              if row.model != PLACEHOLDER_MODEL and not (row.justification or "").startswith("Placeholder")}
    by_company: dict[int, list] = {}
    for r in bind.execute(sa.select(runs).where(runs.c.status == "succeeded")
                          .order_by(runs.c.finished_at, runs.c.id)).mappings():
        by_company.setdefault(r["company_id"], []).append(dict(r))
    for cid, cur_id in bind.execute(sa.select(companies.c.id, companies.c.current_run_id)).all():
        current = None
        for r in by_company.get(cid, []):
            publish, note = decide(bool(r["ranked"]), r["coverage"], current, cid in legacy)
            summary = dict(r["summary"] or {})
            changed = bool(r["published"]) != publish
            if changed or (summary.get("publish_note") != note and not publish):
                summary["publish_note"] = note
                bind.execute(runs.update().where(runs.c.id == r["id"]).values(published=publish, summary=summary))
            if publish:
                current = r
        new_cur = current["id"] if current else None
        # only touch companies whose current run is a v2 run we replayed (or that point at a withheld one)
        if new_cur != cur_id and (cur_id is None or cur_id in {x["id"] for x in by_company.get(cid, [])}):
            bind.execute(companies.update().where(companies.c.id == cid).values(current_run_id=new_cur))


def downgrade() -> None:
    pass
