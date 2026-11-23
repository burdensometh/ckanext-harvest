"""add fetch claim columns to harvest_object

Revision ID: c2a4f1e7b9d3
Revises: 75d650dfd519
Create Date: 2026-11-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c2a4f1e7b9d3"
down_revision = "75d650dfd519"
branch_labels = None
depends_on = None


def upgrade():
    # ``claim_token`` identifies the worker currently processing an object and
    # ``claim_expires`` is the instant after which another worker may take it
    # over. Both are nullable: existing rows (and produced-but-not-yet-fetched
    # objects) simply carry no claim.
    op.add_column(
        "harvest_object",
        sa.Column("claim_token", sa.UnicodeText, nullable=True),
    )
    op.add_column(
        "harvest_object",
        sa.Column("claim_expires", sa.DateTime, nullable=True),
    )


def downgrade():
    op.drop_column("harvest_object", "claim_expires")
    op.drop_column("harvest_object", "claim_token")
