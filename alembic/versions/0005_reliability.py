"""Reliability (pipeline-v2.2): checkpoints, automatic retries, persisted identity, archived sources.

Additive columns only (all nullable, no defaults that rewrite rows):
  judgment_runs.checkpoint        JSON  — per-stage outputs so a retry resumes from the last good stage
  judgment_runs.next_attempt_at   time  — automatic retry not before this time (null = now)
  judgment_runs.error_class       str   — transient | permanent
  judgment_runs.degraded          JSON  — stages that degraded (budget cap, judge fallback, ...)
  companies.identity              JSON  — resolved identity reused across runs (stops flip-flopping)
  companies.identity_status       str   — auto | pinned (admin) | null
  companies.identity_resolved_at  time
  sources.archive_url / archive_timestamp — Wayback Machine copy used when the original refused us
  evidence.note                   text  — why a figure was reclassified (e.g. storage ≠ consumption)

Data fix (idempotent, JSON annotation only): migration 0004 corrected judgment_runs.published,
but the run's aggregate-stage detail and its 'judgment_run' activity-log entry still said
"published": true. Those copies are set to the stored flag, keeping the original value as
"published_at_run_time". Nothing is deleted. Downgrade drops the new columns.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08
"""
from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

runs = sa.table("judgment_runs", sa.column("id", sa.Integer), sa.column("published", sa.Boolean),
                sa.column("status", sa.String))
stages = sa.table("judgment_stages", sa.column("id", sa.Integer), sa.column("run_id", sa.Integer),
                  sa.column("stage", sa.String), sa.column("detail", sa.JSON))
logs = sa.table("ingest_logs", sa.column("id", sa.Integer), sa.column("action", sa.String),
                sa.column("details", sa.JSON))


def upgrade() -> None:
    with op.batch_alter_table("judgment_runs") as b:
        b.add_column(sa.Column("checkpoint", sa.JSON(), nullable=True))
        b.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("error_class", sa.String(16), nullable=True))
        b.add_column(sa.Column("degraded", sa.JSON(), nullable=True))
    with op.batch_alter_table("companies") as b:
        b.add_column(sa.Column("identity", sa.JSON(), nullable=True))
        b.add_column(sa.Column("identity_status", sa.String(16), nullable=True))
        b.add_column(sa.Column("identity_resolved_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("sources") as b:
        b.add_column(sa.Column("archive_url", sa.Text(), nullable=True))
        b.add_column(sa.Column("archive_timestamp", sa.String(14), nullable=True))
    with op.batch_alter_table("evidence") as b:
        b.add_column(sa.Column("note", sa.Text(), nullable=True))

    bind = op.get_bind()
    published = {r.id: bool(r.published) for r in bind.execute(
        sa.select(runs.c.id, runs.c.published).where(runs.c.status == "succeeded"))}
    for row in bind.execute(sa.select(stages.c.id, stages.c.run_id, stages.c.detail)
                            .where(stages.c.stage == "aggregate")).all():
        d = row.detail if isinstance(row.detail, dict) else None
        if d is None or row.run_id not in published or "published" not in d:
            continue
        if bool(d["published"]) != published[row.run_id]:
            nd = {**d, "published": published[row.run_id],
                  "published_at_run_time": d.get("published_at_run_time", d["published"])}
            bind.execute(stages.update().where(stages.c.id == row.id).values(detail=nd))
    for row in bind.execute(sa.select(logs.c.id, logs.c.details).where(logs.c.action == "judgment_run")).all():
        d = row.details if isinstance(row.details, dict) else None
        if d is None or d.get("run_id") not in published or "published" not in d:
            continue
        if bool(d["published"]) != published[d["run_id"]]:
            nd = {**d, "published": published[d["run_id"]],
                  "published_at_run_time": d.get("published_at_run_time", d["published"])}
            bind.execute(logs.update().where(logs.c.id == row.id).values(details=nd))


def downgrade() -> None:
    with op.batch_alter_table("evidence") as b:
        b.drop_column("note")
    with op.batch_alter_table("sources") as b:
        b.drop_column("archive_timestamp")
        b.drop_column("archive_url")
    with op.batch_alter_table("companies") as b:
        b.drop_column("identity_resolved_at")
        b.drop_column("identity_status")
        b.drop_column("identity")
    with op.batch_alter_table("judgment_runs") as b:
        b.drop_column("degraded")
        b.drop_column("error_class")
        b.drop_column("next_attempt_at")
        b.drop_column("checkpoint")
