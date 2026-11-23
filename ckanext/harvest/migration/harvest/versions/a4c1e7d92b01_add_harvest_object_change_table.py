"""add harvest_object_change table

Revision ID: a4c1e7d92b01
Revises: 75d650dfd519
Create Date: 2026-11-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a4c1e7d92b01"
down_revision = "75d650dfd519"
branch_labels = None
depends_on = None


def upgrade():
    engine = op.get_bind()
    inspector = sa.inspect(engine)
    tables = inspector.get_table_names()
    if "harvest_object_change" not in tables:
        op.create_table(
            "harvest_object_change",
            sa.Column("id", sa.UnicodeText, primary_key=True),
            sa.Column(
                "harvest_object_id",
                sa.UnicodeText,
                sa.ForeignKey(
                    "harvest_object.id", ondelete="CASCADE"
                ),
                nullable=False,
            ),
            sa.Column(
                "harvest_job_id",
                sa.UnicodeText,
                sa.ForeignKey("harvest_job.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "harvest_source_id",
                sa.UnicodeText,
                sa.ForeignKey(
                    "harvest_source.id", ondelete="CASCADE"
                ),
                nullable=False,
            ),
            sa.Column(
                "package_id",
                sa.UnicodeText,
                sa.ForeignKey("package.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("guid", sa.UnicodeText),
            sa.Column("change_type", sa.UnicodeText, nullable=False),
            sa.Column("change_details", sa.UnicodeText, nullable=True),
            sa.Column("created", sa.DateTime),
            sa.UniqueConstraint(
                "harvest_object_id",
                name="harvest_object_change_object_id_key",
            ),
        )

    index_names = [
        index["name"]
        for index in inspector.get_indexes("harvest_object_change")
    ]
    indexes = {
        "harvest_object_change_source_id_idx": ["harvest_source_id"],
        "harvest_object_change_job_id_idx": ["harvest_job_id"],
        "harvest_object_change_created_idx": ["created"],
        "harvest_object_change_type_idx": ["change_type"],
        "harvest_object_change_guid_idx": ["guid"],
    }
    for index_name, columns in indexes.items():
        if index_name not in index_names:
            op.create_index(
                index_name, "harvest_object_change", columns
            )


def downgrade():
    op.drop_table("harvest_object_change")
