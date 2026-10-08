"""v2 pipeline: judgment runs, stages, sources, evidence, metrics, category scores.

Additive only: new tables plus nullable columns on companies. Existing rows (companies,
scores, score_history, ingest_logs) are untouched, so legacy pages keep working.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import func


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

ACTIVE = sa.text("status IN ('queued', 'running')")


def upgrade() -> None:
    op.add_column("companies", sa.Column("official_name", sa.String, nullable=True))
    op.add_column("companies", sa.Column("ticker", sa.String(16), nullable=True))
    op.add_column("companies", sa.Column("exchange", sa.String(32), nullable=True))
    op.add_column("companies", sa.Column("cik", sa.String(10), nullable=True))
    op.add_column("companies", sa.Column("is_public", sa.Boolean, nullable=True))
    op.add_column("companies", sa.Column("current_run_id", sa.Integer, nullable=True))
    op.create_index("ix_companies_current_run_id", "companies", ["current_run_id"])

    op.create_table(
        "judgment_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("trigger", sa.String(32)),
        sa.Column("triggered_by", sa.String),
        sa.Column("suggestion_id", sa.Integer, nullable=True),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("worker_id", sa.String(64)),
        sa.Column("current_stage", sa.String(32)),
        sa.Column("pipeline_version", sa.String(32)),
        sa.Column("prompt_version", sa.String(32)),
        sa.Column("rubric_version", sa.String(32)),
        sa.Column("weights_version", sa.String(32)),
        sa.Column("prompt_hash", sa.String(64)),
        sa.Column("model", sa.String),
        sa.Column("model_returned", sa.String),
        sa.Column("budget_usd", sa.Float),
        sa.Column("queued_at", sa.DateTime(timezone=True), server_default=func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("reasoning_tokens", sa.Integer),
        sa.Column("tool_calls", sa.Integer),
        sa.Column("cost_usd", sa.Float),
        sa.Column("index_score", sa.Float),
        sa.Column("measured_score", sa.Float),
        sa.Column("judged_score", sa.Float),
        sa.Column("k_equivalent", sa.Float),
        sa.Column("avg_power_w", sa.Float),
        sa.Column("confidence", sa.Float),
        sa.Column("coverage", sa.Float),
        sa.Column("ranked", sa.Boolean),
        sa.Column("published", sa.Boolean),
        sa.Column("error_type", sa.String(64)),
        sa.Column("error_message", sa.Text),
        sa.Column("summary", sa.JSON),
    )
    op.create_index("ix_judgment_runs_company_id", "judgment_runs", ["company_id"])
    op.create_index("ix_judgment_runs_status", "judgment_runs", ["status"])
    op.create_index("uq_judgment_runs_active_company", "judgment_runs", ["company_id"], unique=True,
                    postgresql_where=ACTIVE, sqlite_where=ACTIVE)

    op.create_table(
        "judgment_stages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("judgment_runs.id"), nullable=False),
        sa.Column("seq", sa.Integer),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("model", sa.String),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("reasoning_tokens", sa.Integer),
        sa.Column("tool_calls", sa.Integer),
        sa.Column("cost_usd", sa.Float),
        sa.Column("error", sa.Text),
        sa.Column("detail", sa.JSON),
    )
    op.create_index("ix_judgment_stages_run_id", "judgment_stages", ["run_id"])

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("judgment_runs.id"), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("final_url", sa.Text),
        sa.Column("origin", sa.String(16)),
        sa.Column("http_status", sa.Integer),
        sa.Column("content_type", sa.String),
        sa.Column("title", sa.Text),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("byte_size", sa.Integer),
        sa.Column("text", sa.Text),
        sa.Column("status", sa.String(16)),
        sa.Column("reject_reason", sa.Text),
        sa.Column("is_primary", sa.Boolean),
    )
    op.create_index("ix_sources_run_id", "sources", ["run_id"])

    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("judgment_runs.id"), nullable=False),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id"), nullable=True),
        sa.Column("category", sa.String(32)),
        sa.Column("kind", sa.String(16)),
        sa.Column("claim", sa.Text),
        sa.Column("quote", sa.Text),
        sa.Column("quote_verified", sa.Boolean),
        sa.Column("context", sa.Text),
        sa.Column("metric_key", sa.String(48)),
        sa.Column("value", sa.Float),
        sa.Column("unit", sa.String(24)),
        sa.Column("period", sa.String(16)),
        sa.Column("scope", sa.Text),
        sa.Column("rejected_reason", sa.Text),
    )
    op.create_index("ix_evidence_run_id", "evidence", ["run_id"])

    op.create_table(
        "metrics",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("judgment_runs.id"), nullable=False),
        sa.Column("metric_key", sa.String(48), nullable=False),
        sa.Column("value", sa.Float),
        sa.Column("unit", sa.String(24)),
        sa.Column("value_si", sa.Float),
        sa.Column("unit_si", sa.String(16)),
        sa.Column("period", sa.String(16)),
        sa.Column("method", sa.Text),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id"), nullable=True),
        sa.Column("evidence_ids", sa.JSON),
    )
    op.create_index("ix_metrics_run_id", "metrics", ["run_id"])

    op.create_table(
        "category_scores",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("judgment_runs.id"), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("family", sa.String(16)),
        sa.Column("score", sa.Float),
        sa.Column("confidence", sa.Float),
        sa.Column("weight", sa.Float),
        sa.Column("rationale", sa.Text),
        sa.Column("evidence_ids", sa.JSON),
        sa.Column("inputs", sa.JSON),
        sa.Column("insufficient", sa.Boolean),
        sa.Column("rubric_version", sa.String(32)),
    )
    op.create_index("ix_category_scores_run_id", "category_scores", ["run_id"])


def downgrade() -> None:
    for table in ("category_scores", "metrics", "evidence", "sources", "judgment_stages"):
        op.drop_table(table)
    op.drop_index("uq_judgment_runs_active_company", table_name="judgment_runs")
    op.drop_table("judgment_runs")
    op.drop_index("ix_companies_current_run_id", table_name="companies")
    with op.batch_alter_table("companies") as batch:
        for col in ("current_run_id", "is_public", "cik", "exchange", "ticker", "official_name"):
            batch.drop_column(col)
