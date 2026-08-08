"""Immutable PHASE 3 FunPay durable ingestion and lot mapping schema."""

import sqlalchemy as sa

from alembic import op

revision = "0004_phase3_funpay"
down_revision = "0003_phase2_security_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_lots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("external_lot_id", sa.String(128), nullable=False),
        sa.Column("enabled_expected", sa.Boolean(), nullable=False),
        sa.Column("safe_metadata", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("external_lot_id", name="uq_account_lot_external"),
    )
    op.create_index("ix_account_lots_account_id", "account_lots", ["account_id"])
    op.create_table(
        "funpay_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("external_event_id", sa.String(180), nullable=False, unique=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("funpay_order_id", sa.String(128)),
        sa.Column("buyer_id", sa.String(128)),
        sa.Column("buyer_handle", sa.String(128)),
        sa.Column("lot_id", sa.String(128)),
        sa.Column("offer_id", sa.String(128)),
        sa.Column("tariff_code", sa.String(64)),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("message_text", sa.Text()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_status", sa.String(32), nullable=False),
        sa.Column("claim_token", sa.String(36)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.String(180), nullable=False),
        sa.Column("safe_metadata", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_funpay_events_event_type", "funpay_events", ["event_type"])
    op.create_index("ix_funpay_events_funpay_order_id", "funpay_events", ["funpay_order_id"])
    op.create_index("ix_funpay_events_processing_status", "funpay_events", ["processing_status"])
    op.create_table(
        "message_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(180), nullable=False, unique=True),
        sa.Column("funpay_event_id", sa.String(36), sa.ForeignKey("funpay_events.id")),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("external_message_id", sa.String(128)),
        sa.Column("delivery_status", sa.String(32), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("ambiguous", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("safe_metadata", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("message_receipts")
    op.drop_index("ix_funpay_events_processing_status", table_name="funpay_events")
    op.drop_index("ix_funpay_events_funpay_order_id", table_name="funpay_events")
    op.drop_index("ix_funpay_events_event_type", table_name="funpay_events")
    op.drop_table("funpay_events")
    op.drop_index("ix_account_lots_account_id", table_name="account_lots")
    op.drop_table("account_lots")
