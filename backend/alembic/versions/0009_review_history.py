"""Persist every changed machine result and manual drafts/OCR artifacts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB
revision = "0009_review_history"
down_revision = "0008_pdf_bindings"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("position_result_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position_index", sa.Integer(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("clock_timestamp()"), nullable=False))
    op.create_index("ix_position_result_versions_run_id", "position_result_versions", ["run_id"])
    op.execute("LOCK TABLE position_results IN SHARE ROW EXCLUSIVE MODE")
    op.execute("INSERT INTO position_result_versions(run_id,position_index,result) SELECT run_id,position_index,result FROM position_results ORDER BY run_id,position_index")
    op.execute("""CREATE OR REPLACE FUNCTION retain_position_result_version() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            INSERT INTO position_result_versions(run_id,position_index,result) VALUES (NEW.run_id,NEW.position_index,NEW.result);
          ELSIF NEW.result IS DISTINCT FROM OLD.result THEN
            INSERT INTO position_result_versions(run_id,position_index,result) VALUES (NEW.run_id,NEW.position_index,NEW.result);
          END IF;
          RETURN NEW;
        END $$""")
    op.execute("CREATE TRIGGER position_result_history AFTER INSERT OR UPDATE OF result ON position_results FOR EACH ROW EXECUTE FUNCTION retain_position_result_version()")
    op.create_table("review_artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("fingerprint", sa.String(64), unique=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_review_artifacts_run_id", "review_artifacts", ["run_id"])

def downgrade():
    op.drop_table("review_artifacts")
    op.execute("DROP TRIGGER position_result_history ON position_results")
    op.execute("DROP FUNCTION retain_position_result_version()")
    op.drop_table("position_result_versions")
