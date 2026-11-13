"""Persist VOR formatting verdict per revision."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_format_status"
down_revision = "0004_drop_manual_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_versions",
        sa.Column(
            "format_status",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("document_versions", "format_status")
