"""add retryable flag to harvest errors

Revision ID: a4c1e7f9b2d3
Revises: 75d650dfd519
Create Date: 2026-11-23 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "a4c1e7f9b2d3"
down_revision = "75d650dfd519"
branch_labels = None
depends_on = None


def upgrade():
    # Existing rows are treated as permanent failures, which preserves the
    # previous behaviour where nothing was automatically retried.
    op.add_column(
        "harvest_gather_error",
        sa.Column("retryable", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.add_column(
        "harvest_object_error",
        sa.Column("retryable", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade():
    op.drop_column("harvest_object_error", "retryable")
    op.drop_column("harvest_gather_error", "retryable")
