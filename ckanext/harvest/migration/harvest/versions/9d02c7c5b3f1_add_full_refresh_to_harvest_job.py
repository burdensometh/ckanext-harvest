"""add full_refresh flag to harvest_job

Revision ID: 9d02c7c5b3f1
Revises: 75d650dfd519
Create Date: 2026-11-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9d02c7c5b3f1"
down_revision = "75d650dfd519"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "harvest_job",
        sa.Column(
            "full_refresh",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    op.drop_column("harvest_job", "full_refresh")
