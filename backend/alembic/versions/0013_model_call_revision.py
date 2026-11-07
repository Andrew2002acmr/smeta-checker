"""Scope model receipts to execution revision; cross-revision reuse is unsafe."""
from alembic import op

revision = '0013_model_call_revision'
down_revision = '0012_audit_graph'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    # Name varies by backend; drop through raw SQL guard.
    connection.exec_driver_sql(
        'ALTER TABLE audit_model_calls DROP CONSTRAINT IF EXISTS '
        'audit_model_calls_run_id_position_index_request_sha_key')
    connection.exec_driver_sql(
        'ALTER TABLE audit_model_calls ADD CONSTRAINT '
        'audit_model_calls_run_position_sha_revision_key UNIQUE '
        '(run_id, position_index, request_sha, execution_revision)')


def downgrade():
    connection = op.get_bind()
    connection.exec_driver_sql(
        'ALTER TABLE audit_model_calls DROP CONSTRAINT IF EXISTS '
        'audit_model_calls_run_position_sha_revision_key')
    # Downgrade keeps newer rows; duplicates across revisions collapse by keeping latest.
    connection.exec_driver_sql(
        'DELETE FROM audit_model_calls a USING audit_model_calls b '
        'WHERE a.run_id = b.run_id AND a.position_index = b.position_index '
        'AND a.request_sha = b.request_sha AND a.created_at < b.created_at')
    connection.exec_driver_sql(
        'ALTER TABLE audit_model_calls ADD CONSTRAINT '
        'audit_model_calls_run_id_position_index_request_sha_key UNIQUE '
        '(run_id, position_index, request_sha)')
