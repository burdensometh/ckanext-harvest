"""add harvest config version

Adds the ``harvest_config_version`` table, which keeps versioned snapshots
of each harvest source's configuration (config string and default owner
org), and the ``harvest_job.config_version_id`` column that pins each job
to the config version confirmed when the job was created.

Revision ID: 8f3a2c1d9e04
Revises: 75d650dfd519
Create Date: 2026-11-23 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8f3a2c1d9e04"
down_revision = "75d650dfd519"
branch_labels = None
depends_on = None


def upgrade():
    engine = op.get_bind()
    inspector = sa.inspect(engine)
    tables = inspector.get_table_names()

    if "harvest_config_version" not in tables:
        op.create_table(
            "harvest_config_version",
            sa.Column("id", sa.UnicodeText, primary_key=True),
            sa.Column(
                "source_id",
                sa.UnicodeText,
                sa.ForeignKey("harvest_source.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("config", sa.UnicodeText),
            sa.Column("owner_org", sa.UnicodeText),
            sa.Column("created", sa.DateTime),
            sa.UniqueConstraint(
                "source_id",
                "version",
                name="harvest_config_version_source_version_key",
            ),
        )
        op.create_index(
            "harvest_config_version_source_id_idx",
            "harvest_config_version",
            ["source_id"],
        )

    columns = [c["name"] for c in inspector.get_columns("harvest_job")]
    if "config_version_id" not in columns:
        op.add_column(
            "harvest_job",
            sa.Column("config_version_id", sa.UnicodeText, nullable=True),
        )
        op.create_foreign_key(
            "harvest_job_config_version_id_fkey",
            "harvest_job",
            "harvest_config_version",
            ["config_version_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade():
    op.drop_constraint(
        "harvest_job_config_version_id_fkey", "harvest_job", type_="foreignkey"
    )
    op.drop_column("harvest_job", "config_version_id")
    op.drop_index(
        "harvest_config_version_source_id_idx",
        table_name="harvest_config_version",
    )
    op.drop_table("harvest_config_version")
