"""Paid API + Stripe billing (1.16.0): api_customers, api_keys, api_entitlements, stripe_events.

Additive only: four new tables. Paying never changes publication or public scores.
Downgrade drops all four.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_api_customers_email", "api_customers", ["email"])
    op.create_index("ix_api_customers_stripe_customer_id", "api_customers", ["stripe_customer_id"], unique=True)
    op.create_index("ix_api_customers_stripe_subscription_id", "api_customers", ["stripe_subscription_id"])

    op.create_table(
        "api_entitlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("api_customers.id"), nullable=False),
        sa.Column("tier", sa.String(32), nullable=False),  # explorer|research|operator|enterprise
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),  # active|past_due|canceled
        sa.Column("reads_per_month", sa.Integer(), nullable=False),
        sa.Column("reads_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ghost_runs_per_month", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stripe_price_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_api_entitlements_customer_id", "api_entitlements", ["customer_id"], unique=True)

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("api_customers.id"), nullable=False),
        sa.Column("key_prefix", sa.String(24), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("label", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),  # active|revoked
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plaintext_once", sa.Text(), nullable=True),  # cleared after success-page reveal
        sa.Column("stripe_checkout_session_id", sa.String(128), nullable=True),
    )
    op.create_index("ix_api_keys_customer_id", "api_keys", ["customer_id"])
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])
    op.create_index("ix_api_keys_stripe_checkout_session_id", "api_keys", ["stripe_checkout_session_id"])

    op.create_table(
        "stripe_events",
        sa.Column("id", sa.String(64), primary_key=True),  # Stripe event id (evt_…)
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("stripe_events")
    op.drop_index("ix_api_keys_stripe_checkout_session_id", table_name="api_keys")
    op.drop_index("ix_api_keys_key_prefix", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_index("ix_api_keys_customer_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_api_entitlements_customer_id", table_name="api_entitlements")
    op.drop_table("api_entitlements")
    op.drop_index("ix_api_customers_stripe_subscription_id", table_name="api_customers")
    op.drop_index("ix_api_customers_stripe_customer_id", table_name="api_customers")
    op.drop_index("ix_api_customers_email", table_name="api_customers")
    op.drop_table("api_customers")
