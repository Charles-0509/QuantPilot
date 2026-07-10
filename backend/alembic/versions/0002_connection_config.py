"""Add encrypted local web connection configuration.

Revision ID: 0002_connection_config
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_connection_config"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The initial migration uses metadata.create_all(), so new installations may
    # already have this table when 0001 executes with the current metadata.
    inspector = sa.inspect(op.get_bind())
    if "connection_config" not in inspector.get_table_names():
        op.create_table(
            "connection_config",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("api_key_cipher", sa.Text(), nullable=False),
            sa.Column("api_secret_cipher", sa.Text(), nullable=False),
            sa.Column("data_feed", sa.String(length=12), nullable=False, server_default="iex"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "connection_config" in inspector.get_table_names():
        op.drop_table("connection_config")
