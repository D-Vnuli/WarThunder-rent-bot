"""PHASE 5 durable normal-worker ownership fencing.

Revision ID: 0006
Revises: 0005_phase4_pixelstorm
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_phase5_normal_worker_fencing"
down_revision = "0005_phase4_pixelstorm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("operations", sa.Column("normal_claim_token", sa.String(length=36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("operations") as batch:
        batch.drop_column("normal_claim_token")
