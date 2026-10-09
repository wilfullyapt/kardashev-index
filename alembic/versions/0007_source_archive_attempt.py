"""Wayback attempt on sources (0.11.0): what the archive fallback did for a source that refused us.

Additive, nullable column only; no data is rewritten:
  sources.archive_attempt  json  — {"lookup", "outcome", "note", "used", "tried"}; null when no
                                   archive fallback was attempted (older runs, readable sources)
Downgrade drops the column.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09
"""
from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as b:
        b.add_column(sa.Column("archive_attempt", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sources") as b:
        b.drop_column("archive_attempt")
