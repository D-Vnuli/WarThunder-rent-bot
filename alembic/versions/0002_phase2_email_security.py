"""Immutable PHASE 2 email-security schema."""

import sqlalchemy as sa

from alembic import op

revision = "0002_phase2_email_security"
down_revision = "0001_phase1_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processed_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_message_id", sa.String(255), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "external_message_id", name="uq_processed_message"),
    )
    op.create_table(
        "classified_email_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("gmail_message_id", sa.String(255), nullable=False, unique=True),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("routing_account_id", sa.String(36), sa.ForeignKey("accounts.id")),
        sa.Column("correlation_operation_id", sa.String(36), sa.ForeignKey("operations.id")),
        sa.Column("claim_token", sa.String(36)),
        sa.Column("claimed_by", sa.String(32)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("payload_state", sa.String(32), nullable=False),
        sa.Column("safe_metadata", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_classified_email_events_message_type", "classified_email_events", ["message_type"]
    )
    op.create_index(
        "ix_classified_email_events_routing_account_id",
        "classified_email_events",
        ["routing_account_id"],
    )
    op.create_index(
        "ix_classified_email_events_correlation_operation_id",
        "classified_email_events",
        ["correlation_operation_id"],
    )
    op.create_table(
        "otp_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("rental_id", sa.String(36), sa.ForeignKey("rentals.id"), nullable=False),
        sa.Column("buyer_id", sa.String(128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column(
            "gmail_message_id", sa.String(255), sa.ForeignKey("classified_email_events.gmail_message_id"), unique=True
        ),
    )
    op.create_index("ix_otp_requests_rental_id", "otp_requests", ["rental_id"])
    op.create_table(
        "security_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("rental_id", sa.String(36), sa.ForeignKey("rentals.id")),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("safe_metadata", sa.Text(), nullable=False),
    )
    op.create_index("ix_security_events_account_id", "security_events", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_security_events_account_id", table_name="security_events")
    op.drop_table("security_events")
    op.drop_index("ix_otp_requests_rental_id", table_name="otp_requests")
    op.drop_table("otp_requests")
    op.drop_index(
        "ix_classified_email_events_correlation_operation_id",
        table_name="classified_email_events",
    )
    op.drop_index(
        "ix_classified_email_events_routing_account_id", table_name="classified_email_events"
    )
    op.drop_index("ix_classified_email_events_message_type", table_name="classified_email_events")
    op.drop_table("classified_email_events")
    op.drop_table("processed_messages")
