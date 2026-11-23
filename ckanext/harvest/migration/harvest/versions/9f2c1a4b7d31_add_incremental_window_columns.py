"""add incremental window columns

Revision ID: 9f2c1a4b7d31
Revises: 75d650dfd519
Create Date: 2026-11-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9f2c1a4b7d31"
down_revision = "75d650dfd519"
branch_labels = None
depends_on = None


def upgrade():
    engine = op.get_bind()
    inspector = sa.inspect(engine)

    source_columns = [
        column["name"] for column in inspector.get_columns("harvest_source")
    ]
    if "incremental_checkpoint" not in source_columns:
        op.add_column(
            "harvest_source",
            sa.Column("incremental_checkpoint", sa.DateTime, nullable=True),
        )

    job_columns = [
        column["name"] for column in inspector.get_columns("harvest_job")
    ]
    if "incremental_since" not in job_columns:
        op.add_column(
            "harvest_job",
            sa.Column("incremental_since", sa.DateTime, nullable=True),
        )
    if "incremental_until" not in job_columns:
        op.add_column(
            "harvest_job",
            sa.Column("incremental_until", sa.DateTime, nullable=True),
        )


def downgrade():
    op.drop_column("harvest_job", "incremental_until")
    op.drop_column("harvest_job", "incremental_since")
    op.drop_column("harvest_source", "incremental_checkpoint")
