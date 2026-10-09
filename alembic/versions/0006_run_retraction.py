"""Run retraction (0.10.0): an admin can retract a published run that is known to be wrong.

Additive, nullable columns only; no data is rewritten:
  judgment_runs.retracted_at       time  — when the run was retracted (null = not retracted)
  judgment_runs.retracted_by       str   — admin who retracted it
  judgment_runs.retraction_reason  text  — why (shown publicly as a correction note)
Every retraction is also written to ingest_logs (action "run_retracted"). Downgrade drops the columns.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09
"""
from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("judgment_runs") as b:
        b.add_column(sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("retracted_by", sa.String(), nullable=True))
        b.add_column(sa.Column("retraction_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("judgment_runs") as b:
        b.drop_column("retraction_reason")
        b.drop_column("retracted_by")
        b.drop_column("retracted_at")
