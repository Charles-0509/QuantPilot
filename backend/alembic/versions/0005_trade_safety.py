"""Track strategy-owned positions and filled quantities.

Revision ID: 0005_trade_safety
Revises: 0004_multi_user
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_trade_safety"
down_revision = "0004_multi_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "orders" in tables:
        order_columns = {column["name"] for column in inspector.get_columns("orders")}
        if "filled_qty" not in order_columns:
            with op.batch_alter_table("orders") as batch:
                batch.add_column(
                    sa.Column("filled_qty", sa.Float(), nullable=False, server_default="0")
                )
            with op.batch_alter_table("orders") as batch:
                batch.alter_column("filled_qty", server_default=None)

    if "strategy_positions" not in tables:
        op.create_table(
            "strategy_positions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("strategy_id", sa.String(length=36), nullable=False),
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("qty", sa.Float(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["auth_users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "strategy_id", "symbol", name="uq_strategy_position"),
        )
        op.create_index("ix_strategy_positions_user_id", "strategy_positions", ["user_id"])
        op.create_index(
            "ix_strategy_positions_strategy_id", "strategy_positions", ["strategy_id"]
        )
        op.create_index("ix_strategy_positions_symbol", "strategy_positions", ["symbol"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "strategy_positions" in inspector.get_table_names():
        op.drop_table("strategy_positions")
    if "orders" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("orders")}
        if "filled_qty" in columns:
            with op.batch_alter_table("orders") as batch:
                batch.drop_column("filled_qty")
