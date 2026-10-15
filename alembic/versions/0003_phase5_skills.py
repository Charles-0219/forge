"""Phase 5 skill versions and evaluation reports."""

import sqlalchemy as sa
from alembic import op


revision = "0003_phase5"
down_revision = "0002_phase3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("scope", sa.String(400), nullable=False),
        sa.Column("active_version_id", sa.String(36), nullable=True),
        sa.Column("export_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.UniqueConstraint("scope", "name", name="uq_skills_scope_name"),
    )
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("parent_version_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("suite_ref", sa.String(200), nullable=True),
        sa.Column("block_reason", sa.String(200), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "eval_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("skill_versions.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("baseline", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=True),
        sa.Column("promoted", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.String(64), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("eval_reports")
    op.drop_table("skill_versions")
    op.drop_table("skills")
