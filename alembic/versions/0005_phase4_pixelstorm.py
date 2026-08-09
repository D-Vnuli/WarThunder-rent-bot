"""phase4 Pixel Storm maintenance authentication timestamp

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_phase4_pixelstorm"
down_revision = "0004_phase3_funpay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operations", sa.Column("maintenance_login_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("operations", sa.Column("password_change_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("operations", sa.Column("security_state", sa.String(length=48), nullable=False, server_default="INIT"))
    op.add_column("operations", sa.Column("recovery_claim_token", sa.String(length=36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("operations") as batch:
        batch.drop_column("recovery_claim_token")
        batch.drop_column("security_state")
        batch.drop_column("maintenance_login_requested_at")
        batch.drop_column("password_change_requested_at")
