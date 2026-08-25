"""Add cash sweep settings to risk_settings.

Revision ID: 0007_cash_sweep
Revises: 0006_execution_incidents
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_cash_sweep"
down_revision = "0006_execution_incidents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = [c["name"] for c in inspector.get_columns("risk_settings")]
    if "cash_sweep_enabled" not in columns:
        op.add_column(
            "risk_settings",
            sa.Column(
                "cash_sweep_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    if "cash_sweep_symbol" not in columns:
        op.add_column(
            "risk_settings",
            sa.Column(
                "cash_sweep_symbol",
                sa.String(length=16),
                nullable=False,
                server_default="SGOV",
            ),
        )
    if "cash_sweep_buffer_pct" not in columns:
        op.add_column(
            "risk_settings",
            sa.Column(
                "cash_sweep_buffer_pct",
                sa.Float(),
                nullable=False,
                server_default="2.0",
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = [c["name"] for c in inspector.get_columns("risk_settings")]
    if "cash_sweep_enabled" in columns:
        op.drop_column("risk_settings", "cash_sweep_enabled")
    if "cash_sweep_symbol" in columns:
        op.drop_column("risk_settings", "cash_sweep_symbol")
    if "cash_sweep_buffer_pct" in columns:
        op.drop_column("risk_settings", "cash_sweep_buffer_pct")
