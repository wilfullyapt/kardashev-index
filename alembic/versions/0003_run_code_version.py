"""Record the deployed code version (git commit) on every judgment run.

Additive: one nullable column. Existing runs keep NULL ("unknown").

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08
"""
from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("judgment_runs", sa.Column("code_version", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("judgment_runs") as batch:
        batch.drop_column("code_version")
