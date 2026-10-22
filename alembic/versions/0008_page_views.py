"""First-party visit counts (1.15.0): page_views (raw, kept ANALYTICS_RAW_DAYS) and page_view_daily.

Additive only: two new tables, no existing table or row is touched. No raw IP address or full user
agent is stored; ``visitor`` is a daily-salted hash (see app/analytics.py).
Downgrade drops both tables.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-22
"""
from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "page_views",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("path", sa.String(200), nullable=False),
        sa.Column("ref_host", sa.String(120), nullable=True),
        sa.Column("utm_source", sa.String(64), nullable=True),
        sa.Column("utm_medium", sa.String(64), nullable=True),
        sa.Column("utm_campaign", sa.String(64), nullable=True),
        sa.Column("country", sa.String(2), nullable=True),
        sa.Column("ua_class", sa.String(8), nullable=False),
        sa.Column("visitor", sa.String(16), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
    )
    op.create_index("ix_page_views_ts", "page_views", ["ts"])
    op.create_index("ix_page_views_day", "page_views", ["day"])
    op.create_table(
        "page_view_daily",
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("dim", sa.String(12), primary_key=True),
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("ua_class", sa.String(8), primary_key=True),
        sa.Column("views", sa.Integer(), nullable=False),
        sa.Column("visitors", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("page_view_daily")
    op.drop_index("ix_page_views_day", table_name="page_views")
    op.drop_index("ix_page_views_ts", table_name="page_views")
    op.drop_table("page_views")
