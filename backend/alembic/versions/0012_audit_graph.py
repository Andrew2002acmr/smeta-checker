"""Durable graph checkpoints and paid model attempt ledger."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = '0012_audit_graph'
down_revision = '0011_analysis_artifacts'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    url = connection.engine.url
    dsn = url._replace(drivername='postgresql').render_as_string(hide_password=False)
    import psycopg
    from psycopg.rows import dict_row
    from langgraph.checkpoint.postgres import PostgresSaver

    with psycopg.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row,
                         options='-c search_path=audit_graph,public') as saver_connection:
        with saver_connection.cursor() as cursor:
            cursor.execute('CREATE SCHEMA IF NOT EXISTS audit_graph')
        PostgresSaver(saver_connection).setup()
        with saver_connection.cursor() as cursor:
            cursor.execute("SELECT count(*) AS n FROM pg_tables WHERE schemaname='audit_graph'")
            if (cursor.fetchone() or {}).get('n', 0) < 4:
                raise RuntimeError('PostgresSaver.setup did not create checkpoint tables')
    op.create_table(
        'audit_model_calls',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('run_id', UUID(as_uuid=True), sa.ForeignKey('runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('position_index', sa.Integer(), nullable=False),
        sa.Column('request_sha', sa.String(64), nullable=False),
        sa.Column('attempt_no', sa.Integer(), nullable=False),
        sa.Column('execution_revision', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('error_code', sa.String(16), nullable=True),
        sa.Column('reserved_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('actual_tokens', sa.Integer(), nullable=True),
        sa.Column('request_ref', JSONB(), nullable=False, server_default='{}'),
        sa.Column('response_ref', JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('run_id', 'position_index', 'request_sha'),
    )
    op.create_index('ix_audit_model_calls_run_position', 'audit_model_calls', ['run_id', 'position_index'])


def downgrade():
    op.drop_index('ix_audit_model_calls_run_position', table_name='audit_model_calls')
    op.drop_table('audit_model_calls')
    op.execute('DROP SCHEMA IF EXISTS audit_graph CASCADE')
