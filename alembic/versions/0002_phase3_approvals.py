"""Phase 3 approval binding columns."""

import sqlalchemy as sa
from alembic import op


revision = "0002_phase3"
down_revision = "0001_phase2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("arguments_hash", sa.String(length=64), nullable=True))
    op.add_column("approvals", sa.Column("arguments", sa.JSON(), nullable=True))
    op.add_column("approvals", sa.Column("status", sa.String(length=32), nullable=True))
    op.add_column("approvals", sa.Column("basis", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.drop_column("basis")
        batch.drop_column("status")
        batch.drop_column("arguments")
        batch.drop_column("arguments_hash")
