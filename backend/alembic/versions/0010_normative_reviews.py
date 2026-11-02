"""Retain pinned normative searches and engineer selections."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0010_normative_reviews"
down_revision = "0009_review_history"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("normative_reviews",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_normative_reviews_run_id", "normative_reviews", ["run_id"])


def downgrade():
    op.drop_table("normative_reviews")
