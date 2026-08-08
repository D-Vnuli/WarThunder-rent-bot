"""Immutable PHASE 2 durable security-event dispatch state."""

import sqlalchemy as sa

from alembic import op

revision = "0003_phase2_security_dispatch"
down_revision = "0002_phase2_email_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "classified_email_events",
        sa.Column("security_processing_state", sa.String(32), nullable=False, server_default="PENDING"),
    )
    op.add_column("classified_email_events", sa.Column("security_claim_token", sa.String(36)))
    op.add_column(
        "classified_email_events", sa.Column("security_claimed_at", sa.DateTime(timezone=True))
    )
    op.create_index(
        "ix_classified_email_events_security_processing_state",
        "classified_email_events",
        ["security_processing_state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_classified_email_events_security_processing_state",
        table_name="classified_email_events",
    )
    op.drop_column("classified_email_events", "security_claimed_at")
    op.drop_column("classified_email_events", "security_claim_token")
    op.drop_column("classified_email_events", "security_processing_state")
