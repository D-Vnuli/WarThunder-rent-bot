"""Immutable PHASE 1 schema."""

import sqlalchemy as sa

from alembic import op

revision = "0001_phase1_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("rotation_state", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounts_status", "accounts", ["status"])
    op.create_table(
        "orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("funpay_order_id", sa.String(128), nullable=False, unique=True),
        sa.Column("buyer_id", sa.String(128), nullable=False),
        sa.Column("tariff_code", sa.String(64), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id")),
        sa.Column("fulfillment_status", sa.String(32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("safe_metadata", sa.Text(), nullable=False),
    )
    op.create_table(
        "rentals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "order_id", sa.String(36), sa.ForeignKey("orders.id"), nullable=False, unique=True
        ),
        sa.Column("buyer_id", sa.String(128), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("tariff_code", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_rentals_status", "rentals", ["status"])
    op.create_index("ix_rentals_expires_at", "rentals", ["expires_at"])
    op.create_table(
        "operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(180), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("rental_id", sa.String(36), sa.ForeignKey("rentals.id")),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id")),
        sa.Column("correlation_id", sa.String(180), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("safe_metadata", sa.Text(), nullable=False),
    )
    op.create_index("ix_operations_status", "operations", ["status"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(36)),
        sa.Column("rental_id", sa.String(36)),
        sa.Column("correlation_id", sa.String(180), nullable=False),
        sa.Column("safe_metadata", sa.Text(), nullable=False),
        sa.UniqueConstraint("event_type", "correlation_id", name="uq_audit_correlation"),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_index("ix_operations_status", table_name="operations")
    op.drop_table("operations")
    op.drop_index("ix_rentals_expires_at", table_name="rentals")
    op.drop_index("ix_rentals_status", table_name="rentals")
    op.drop_table("rentals")
    op.drop_table("orders")
    op.drop_index("ix_accounts_status", table_name="accounts")
    op.drop_table("accounts")
