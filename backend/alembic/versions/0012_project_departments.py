"""Project departments and persistent document scope."""

from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0012_project_departments"
down_revision = "0011_analysis_artifacts"
branch_labels = None
depends_on = None

# Frozen migration data: later product renames must not change historical upgrades.
DEPARTMENTS = (
    ("Электро-технический отдел", "zap"),
    ("Отдел водоснабжения и водоотведения", "droplets"),
    ("Сметный отдел", "calculator"),
    ("Отдел экологии", "leaf"),
    ("Строительный отдел", "building-2"),
    ("Служба проектирования автомобильных дорог и мостов", "route"),
    ("Служба изысканий", "map-pinned"),
)


def upgrade():
    departments = op.create_table(
        "departments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("icon", sa.String(40), nullable=False, server_default="folder-open"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "name", name="uq_department_name"),
        sa.UniqueConstraint("id", "project_id", name="uq_department_project"),
    )
    op.create_index("ix_departments_project_id", "departments", ["project_id"])
    op.add_column("documents", sa.Column("department_id", UUID(as_uuid=True), nullable=True))
    bind = op.get_bind()
    for project_id in bind.execute(sa.text("SELECT id FROM projects")).scalars():
        for order, (name, icon) in enumerate(DEPARTMENTS):
            department_id = uuid4()
            bind.execute(departments.insert().values(id=department_id, project_id=project_id, name=name, icon=icon, sort_order=order))
            if name == "Служба изысканий":
                bind.execute(sa.text("UPDATE documents SET department_id = :department WHERE project_id = :project"), {"department": department_id, "project": project_id})
    op.alter_column("documents", "department_id", nullable=False)
    op.create_index("ix_documents_department_id", "documents", ["department_id"])
    op.create_foreign_key("fk_documents_department_project", "documents", "departments", ["department_id", "project_id"], ["id", "project_id"], ondelete="RESTRICT")


def downgrade():
    op.drop_constraint("fk_documents_department_project", "documents", type_="foreignkey")
    op.drop_index("ix_documents_department_id", table_name="documents")
    op.drop_column("documents", "department_id")
    op.drop_table("departments")
