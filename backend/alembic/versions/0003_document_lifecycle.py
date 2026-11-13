"""Lifecycle: stable documents, per-version metadata, bindings, pins, archive."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_document_lifecycle"
down_revision = "0002_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("projects", "name", existing_type=sa.String(256), nullable=False)
    op.add_column(
        "projects",
        sa.Column("archived", sa.Boolean(), server_default="false", nullable=False),
    )

    op.drop_constraint(
        "documents_project_id_kind_filename_key", "documents", type_="unique"
    )
    op.add_column(
        "documents",
        sa.Column("title", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "documents",
        sa.Column("archived", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "documents",
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "document_versions",
        sa.Column("filename", sa.String(1024), server_default="", nullable=False),
    )
    op.alter_column("document_versions", "filename", server_default=None)
    op.add_column(
        "document_versions",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "document_versions",
        sa.Column(
            "metadata_overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_document_versions_id_document", "document_versions", ["id", "document_id"]
    )

    # Backfill: legacy per-document filenames move onto their own version
    # rows. Versions (and their UUIDs) never move between documents; identity
    # split for same-filename uploads happens lazily for new uploads only,
    # so existing snapshots stay byte-identical.
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE document_versions v
            SET filename = d.filename
            FROM documents d
            WHERE v.document_id = d.id
            """
        )
    )
    connection.execute(sa.text("UPDATE documents SET title = filename"))
    op.drop_column("documents", "filename")

    # Per-VOR bindings (unconfirmed until an explicit PUT) and relational
    # run pins for exact PDF snapshots.
    op.create_table(
        "vor_bindings",
        sa.Column(
            "vor_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("confirmed", sa.Boolean(), server_default="false", nullable=False),
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
    op.create_table(
        "run_pdf_versions",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_key", sa.String(1024), primary_key=True),
        sa.Column(
            "version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.UniqueConstraint("run_id", "version_id"),
    )

    # Backfill relational pins from the worker execution input
    # (config.pdf_versions). Idempotent: only fills rows missing entirely.
    connection.execute(
        sa.text(
            """
            INSERT INTO run_pdf_versions (run_id, source_key, version_id)
            SELECT r.id, kv.key, kv.value::uuid
            FROM runs r,
                 LATERAL jsonb_each_text(COALESCE(r.config->'pdf_versions', '{}'::jsonb)) AS kv(key, value)
            WHERE NOT EXISTS (
                SELECT 1 FROM run_pdf_versions p WHERE p.run_id = r.id
            )
            ON CONFLICT (run_id, source_key) DO NOTHING
            """
        )
    )

    # Auto-current per document: latest VOR revision, first PDF revision.
    connection.execute(
        sa.text(
            """
            WITH vor AS (
                SELECT DISTINCT ON (v.document_id) v.document_id, v.id
                FROM document_versions v
                JOIN documents d ON d.id = v.document_id
                WHERE d.kind = 'vor'
                ORDER BY v.document_id, v.version_no DESC
            ),
            pdf AS (
                SELECT DISTINCT ON (v.document_id) v.document_id, v.id
                FROM document_versions v
                JOIN documents d ON d.id = v.document_id
                WHERE d.kind = 'pdf'
                ORDER BY v.document_id, v.version_no ASC
            )
            UPDATE documents d
            SET current_version_id = COALESCE(
                (SELECT id FROM vor WHERE vor.document_id = d.id),
                (SELECT id FROM pdf WHERE pdf.document_id = d.id)
            )
            WHERE current_version_id IS NULL
            """
        )
    )

    op.create_foreign_key(
        "fk_documents_current_version",
        "documents",
        "document_versions",
        ["current_version_id", "id"],
        ["id", "document_id"],
        ondelete="RESTRICT",
    )
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


def downgrade() -> None:
    for table in ("vor_bindings", "vor_binding_pdfs", "run_pdf_versions"):
        op.execute(f"DROP TRIGGER document_scope ON {table}")
    op.execute("DROP FUNCTION enforce_document_scope()")
    op.drop_constraint("fk_documents_current_version", "documents", type_="foreignkey")
    op.add_column(
        "documents",
        sa.Column("filename", sa.String(1024), server_default="", nullable=False),
    )
    op.execute("""
        UPDATE documents d SET filename = v.filename
        FROM document_versions v WHERE v.id = d.current_version_id
    """)
    op.drop_table("run_pdf_versions")
    op.drop_table("vor_binding_pdfs")
    op.drop_table("vor_bindings")
    op.drop_constraint(
        "uq_document_versions_id_document", "document_versions", type_="unique"
    )
    op.drop_column("document_versions", "metadata_overrides")
    op.drop_column("document_versions", "metadata")
    op.drop_column("document_versions", "filename")
    op.drop_column("documents", "current_version_id")
    op.drop_column("documents", "archived")
    op.drop_column("documents", "title")
    op.create_unique_constraint(
        "documents_project_id_kind_filename_key",
        "documents",
        ["project_id", "kind", "filename"],
    )
    op.drop_column("projects", "archived")
