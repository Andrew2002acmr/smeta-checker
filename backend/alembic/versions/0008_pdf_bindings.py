"""Explicit version-scoped VOR reference to PDF bindings."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = "0008_pdf_bindings"
down_revision = "0007_human_basis"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("document_versions", sa.Column("pdf_bindings", JSONB(), nullable=False, server_default="{}"))

def downgrade():
    op.drop_column("document_versions", "pdf_bindings")
