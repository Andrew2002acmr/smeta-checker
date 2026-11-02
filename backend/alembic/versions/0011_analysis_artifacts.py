"""Versioned, project-scoped reusable analysis stages."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB,UUID

revision='0011_analysis_artifacts'
down_revision='0010_normative_reviews'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('analysis_artifacts',
        sa.Column('key',sa.String(64),primary_key=True),
        sa.Column('project_id',UUID(as_uuid=True),sa.ForeignKey('projects.id',ondelete='CASCADE'),nullable=False),
        sa.Column('stage',sa.String(32),nullable=False),
        sa.Column('identity',JSONB(),nullable=False),
        sa.Column('payload',JSONB(),nullable=False),
        sa.Column('sha256',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False))
    op.create_index('ix_analysis_artifacts_project_id','analysis_artifacts',['project_id'])


def downgrade():
    op.drop_table('analysis_artifacts')
