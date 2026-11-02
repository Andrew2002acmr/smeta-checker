"""Durable task delivery and execution leases."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_task_outbox"
down_revision = "0005_format_status"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_key", sa.String(128), nullable=False),
        sa.Column("task_name", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("delivery_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("run_id", "task_key"),
    )
    op.create_index("ix_task_outbox_run_id", "task_outbox", ["run_id"])
    op.create_index("ix_task_outbox_status", "task_outbox", ["status"])


def downgrade():
    op.drop_table("task_outbox")
