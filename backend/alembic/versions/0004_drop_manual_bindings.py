"""Drop manual VOR->PDF bindings; designations verify against PDF text."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_drop_manual_bindings"
down_revision = "0003_document_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER document_scope ON vor_bindings")
    op.execute("DROP TRIGGER document_scope ON vor_binding_pdfs")
    op.execute("DROP TRIGGER document_scope ON run_pdf_versions")
    op.execute("DROP FUNCTION enforce_document_scope()")
    op.execute("""
        CREATE OR REPLACE FUNCTION enforce_document_scope() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'run_pdf_versions' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM runs r JOIN document_versions v ON v.id = NEW.version_id
                    JOIN documents d ON d.id = v.document_id
                    WHERE r.id = NEW.run_id AND d.kind = 'pdf'
                        AND d.project_id = r.project_id
                ) THEN
                    RAISE EXCEPTION 'pinned PDF must belong to the run project'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER document_scope BEFORE INSERT OR UPDATE ON run_pdf_versions
        FOR EACH ROW EXECUTE FUNCTION enforce_document_scope()
    """)
    op.drop_table("vor_binding_pdfs")
    op.drop_table("vor_bindings")


def downgrade() -> None:
    op.create_table(
        "vor_bindings",
        sa.Column(
            "vor_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "confirmed",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )
    op.create_table(
        "vor_binding_pdfs",
        sa.Column(
            "vor_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vor_bindings.vor_version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "pdf_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
    )
    op.execute("DROP TRIGGER document_scope ON run_pdf_versions")
    op.execute("DROP FUNCTION enforce_document_scope()")
    op.execute("""
        CREATE OR REPLACE FUNCTION enforce_document_scope() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'vor_bindings' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM document_versions v JOIN documents d ON d.id = v.document_id
                    WHERE v.id = NEW.vor_version_id AND d.kind = 'vor'
                ) THEN
                    RAISE EXCEPTION 'binding source must be a VOR revision'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'vor_binding_pdfs' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM document_versions v JOIN documents d ON d.id = v.document_id
                    JOIN document_versions p ON p.id = NEW.pdf_version_id
                    JOIN documents pd ON pd.id = p.document_id
                    WHERE v.id = NEW.vor_version_id AND d.kind = 'vor'
                        AND pd.kind = 'pdf' AND d.project_id = pd.project_id
                ) THEN
                    RAISE EXCEPTION 'binding revisions must belong to the same project'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'run_pdf_versions' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM runs r JOIN document_versions v ON v.id = NEW.version_id
                    JOIN documents d ON d.id = v.document_id
                    WHERE r.id = NEW.run_id AND d.kind = 'pdf'
                        AND d.project_id = r.project_id
                ) THEN
                    RAISE EXCEPTION 'pinned PDF must belong to the run project'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """)
    for table in ("vor_bindings", "vor_binding_pdfs", "run_pdf_versions"):
        op.execute(f"""
            CREATE TRIGGER document_scope BEFORE INSERT OR UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION enforce_document_scope()
        """)
