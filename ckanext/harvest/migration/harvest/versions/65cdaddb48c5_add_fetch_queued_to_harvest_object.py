"""add fetch_queued to harvest_object

Revision ID: 65cdaddb48c5
Revises: 75d650dfd519
Create Date: 2026-11-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "65cdaddb48c5"
down_revision = "75d650dfd519"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "harvest_object",
        sa.Column("fetch_queued", sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_column("harvest_object", "fetch_queued")
