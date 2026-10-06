"""Initial migration: create all tables (companies, suggestions, scores, ingest_logs, score_history)

Revision ID: 0001
Revises: 
Create Date: 2026-09-27

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import func


# revision identifiers, used by Alembic.
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # companies
    op.create_table(
        'companies',
        sa.Column('id', sa.Integer, primary_key=True, index=True),
        sa.Column('canonical_name', sa.String, unique=True, index=True, nullable=False),
        sa.Column('domain', sa.String, nullable=True),
        sa.Column('wikipedia_url', sa.String, nullable=True),
        sa.Column('crunchbase_id', sa.String, nullable=True),
        sa.Column('hq', sa.String, nullable=True),
        sa.Column('industry', sa.String, nullable=True),
        sa.Column('employee_count', sa.String, nullable=True),
        sa.Column('funding_stage', sa.String, nullable=True),
        sa.Column('suggestion_attempts', sa.Integer, default=0, nullable=False),
        sa.Column('last_ingested_at', sa.DateTime(timezone=True), server_default=func.now()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=func.now()),
    )

    # suggestions
    op.create_table(
        'suggestions',
        sa.Column('id', sa.Integer, primary_key=True, index=True),
        sa.Column('name', sa.String, index=True, nullable=False),
        sa.Column('domain', sa.String, nullable=True, index=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), server_default=func.now()),
        sa.Column('status', sa.String, default='pending', nullable=False),
        sa.Column('denial_reason', sa.Text, nullable=True),
        sa.Column('admin_id', sa.String, nullable=True),
    )

    # scores
    op.create_table(
        'scores',
        sa.Column('id', sa.Integer, primary_key=True, index=True),
        sa.Column('company_id', sa.Integer, sa.ForeignKey('companies.id'), nullable=False),
        sa.Column('category', sa.String, index=True, nullable=False),
        sa.Column('score', sa.Float, nullable=False),
        sa.Column('justification', sa.Text, nullable=True),
        sa.Column('evidence_links', sa.JSON, nullable=True),
        sa.Column('model', sa.String, nullable=True),
        sa.Column('model_version', sa.String, nullable=True),
        sa.Column('judged_at', sa.DateTime(timezone=True), server_default=func.now()),
    )

    # ingest_logs
    op.create_table(
        'ingest_logs',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('company_id', sa.Integer, sa.ForeignKey('companies.id'), nullable=True),
        sa.Column('suggestion_id', sa.Integer, sa.ForeignKey('suggestions.id'), nullable=True),
        sa.Column('action', sa.String, nullable=False),
        sa.Column('details', sa.JSON, nullable=True),
        sa.Column('admin_id', sa.String, nullable=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=func.now()),
    )

    # score_history
    op.create_table(
        'score_history',
        sa.Column('id', sa.Integer, primary_key=True, index=True),
        sa.Column('company_id', sa.Integer, sa.ForeignKey('companies.id'), nullable=False),
        sa.Column('category', sa.String, index=True, nullable=False),
        sa.Column('score', sa.Float, nullable=False),
        sa.Column('justification', sa.Text),
        sa.Column('evidence_links', sa.JSON),
        sa.Column('model', sa.String),
        sa.Column('model_version', sa.String),
        sa.Column('judged_at', sa.DateTime(timezone=True)),
        sa.Column('archived_at', sa.DateTime(timezone=True), server_default=func.now()),
    )


def downgrade() -> None:
    op.drop_table('score_history')
    op.drop_table('ingest_logs')
    op.drop_table('scores')
    op.drop_table('suggestions')
    op.drop_table('companies')
