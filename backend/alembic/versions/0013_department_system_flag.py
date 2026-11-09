"""System department flag; department archive state removed."""

from alembic import op
import sqlalchemy as sa

revision = "0013_department_system_flag"
down_revision = "0012_project_departments"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "departments",
        sa.Column(
            "is_system",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.execute(sa.text("UPDATE departments SET is_system = true"))
    # Fresh projects seed system departments via storage; the flag is never NULL.
    op.alter_column("departments", "is_system", server_default=None)
    # Department archiving is removed: only the parent project carries
    # archived state, child rows are effectively archived through it.
    op.drop_column("departments", "archived")


def downgrade():
    op.add_column(
        "departments",
        sa.Column(
            "archived",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.drop_column("departments", "is_system")
