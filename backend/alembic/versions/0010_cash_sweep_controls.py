"""Enforceable cash sweep pause, cooldown and minimum trade size.

Revision ID: 0010_cash_sweep_controls
Revises: 0009_broker_settings
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_cash_sweep_controls"
down_revision = "0009_broker_settings"
branch_labels = None
depends_on = None

COLUMNS = (
    sa.Column("cash_sweep_paused_until", sa.DateTime(timezone=True), nullable=True),
    sa.Column("cash_sweep_cooldown_minutes", sa.Integer(), nullable=False, server_default="60"),
    sa.Column("cash_sweep_min_trade_usd", sa.Float(), nullable=False, server_default="1000"),
)


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("risk_settings")}
    for column in COLUMNS:
        if column.name not in existing:
            op.add_column("risk_settings", column.copy())


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("risk_settings")}
    for column in COLUMNS:
        if column.name in existing:
            op.drop_column("risk_settings", column.name)
