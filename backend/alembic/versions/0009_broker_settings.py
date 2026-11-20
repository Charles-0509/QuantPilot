"""Broker selection and one-use IBKR live-account consent."""

from alembic import op
from sqlalchemy import inspect
from app.models import BrokerSettings, BrokerLiveChallenge

revision = "0009_broker_settings"
down_revision = "0008_backtest_artifacts"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = inspect(bind).get_table_names()
    for table in (BrokerSettings.__table__, BrokerLiveChallenge.__table__):
        if table.name not in existing:
            table.create(bind)


def downgrade():
    op.drop_table("broker_live_challenges")
    op.drop_table("broker_settings")
