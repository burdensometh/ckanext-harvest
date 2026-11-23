"""create harvest quarantine table

Revision ID: 9c1f4a7be2d3
Revises: 75d650dfd519
Create Date: 2026-11-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9c1f4a7be2d3"
down_revision = "75d650dfd519"
branch_labels = None
depends_on = None


def upgrade():
    engine = op.get_bind()
    inspector = sa.inspect(engine)
    tables = inspector.get_table_names()
    if "harvest_quarantine" in tables:
        return

    op.create_table(
        "harvest_quarantine",
        sa.Column("id", sa.UnicodeText, primary_key=True),
        sa.Column("guid", sa.UnicodeText),
        sa.Column("content", sa.UnicodeText, nullable=True),
        sa.Column("violations", sa.UnicodeText, nullable=True),
        sa.Column("status", sa.UnicodeText),
        sa.Column("package_id", sa.UnicodeText, nullable=True),
        sa.Column("created", sa.DateTime),
        sa.Column("updated", sa.DateTime),
        sa.Column(
            "harvest_object_id",
            sa.UnicodeText,
            sa.ForeignKey("harvest_object.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "harvest_source_id",
            sa.UnicodeText,
            sa.ForeignKey("harvest_source.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "harvest_job_id",
            sa.UnicodeText,
            sa.ForeignKey("harvest_job.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.create_index(
        "harvest_quarantine_source_id_idx",
        "harvest_quarantine",
        ["harvest_source_id"],
    )
    op.create_index(
        "harvest_quarantine_guid_idx",
        "harvest_quarantine",
        ["guid"],
    )


def downgrade():
    op.drop_index("harvest_quarantine_guid_idx", "harvest_quarantine")
    op.drop_index("harvest_quarantine_source_id_idx", "harvest_quarantine")
    op.drop_table("harvest_quarantine")
