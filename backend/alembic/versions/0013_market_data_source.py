"""Market data source independent of the execution broker.

Revision ID: 0013_market_data_source
Revises: 0012_llm_provider
"""

from alembic import op
import sqlalchemy as sa

revision = "0013_market_data_source"
down_revision = "0012_llm_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("broker_settings")}
    if "market_data_source" not in columns:
        op.add_column("broker_settings", sa.Column("market_data_source", sa.String(16), nullable=False,
                                                   server_default="alpaca"))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("broker_settings")}
    if "market_data_source" in columns:
        op.drop_column("broker_settings", "market_data_source")
