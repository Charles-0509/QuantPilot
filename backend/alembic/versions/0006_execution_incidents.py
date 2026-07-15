"""Persist long-only execution quarantines.

Revision ID: 0006_execution_incidents
Revises: 0005_trade_safety
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_execution_incidents"
down_revision = "0005_trade_safety"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "execution_incidents" in inspector.get_table_names():
        return
    op.create_table(
        "execution_incidents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("trigger_order_id", sa.String(length=64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["auth_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "symbol", name="uq_execution_incident_user_symbol"
        ),
    )
    op.create_index(
        "ix_execution_incidents_user_id",
        "execution_incidents",
        ["user_id"],
    )
    op.create_index(
        "ix_execution_incidents_symbol",
        "execution_incidents",
        ["symbol"],
    )
    op.create_index(
        "ix_execution_incidents_status",
        "execution_incidents",
        ["status"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "execution_incidents" in inspector.get_table_names():
        op.drop_table("execution_incidents")
