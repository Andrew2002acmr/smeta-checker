"""Persist reviewed evidence without rewriting automatic audit results."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0007_human_basis"
down_revision = "0006_task_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "human_basis",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position_index", sa.Integer(), nullable=False),
        sa.Column(
            "vor_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "pdf_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position_key", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(256), nullable=True),
    )
    op.create_index("ix_human_basis_project_id", "human_basis", ["project_id"])
    op.create_index("ix_human_basis_position_key", "human_basis", ["position_key"])


def downgrade():
    op.drop_table("human_basis")
