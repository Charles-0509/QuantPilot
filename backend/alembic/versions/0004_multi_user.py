"""Add per-user ownership and administrator-managed accounts.

Revision ID: 0004_multi_user
Revises: 0003_auth
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_multi_user"
down_revision = "0003_auth"
branch_labels = None
depends_on = None


def columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def add_user_column(
    table: str,
    column_name: str = "user_id",
    *,
    nullable: bool = False,
    unique: bool = False,
) -> None:
    if column_name in columns(table):
        return
    with op.batch_alter_table(table) as batch:
        batch.add_column(sa.Column(column_name, sa.Integer(), nullable=nullable, server_default="1"))
        batch.create_foreign_key(
            f"fk_{table}_{column_name}_auth_users",
            "auth_users",
            [column_name],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index(f"ix_{table}_{column_name}", [column_name], unique=unique)
    if nullable:
        op.execute(sa.text(f"UPDATE {table} SET {column_name} = NULL WHERE 1 = 0"))
    with op.batch_alter_table(table) as batch:
        batch.alter_column(column_name, server_default=None)


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "auth_users" in tables and "role" not in columns("auth_users"):
        with op.batch_alter_table("auth_users") as batch:
            batch.add_column(sa.Column("role", sa.String(length=16), nullable=False, server_default="user"))
            batch.create_index("ix_auth_users_role", ["role"])
        op.execute(sa.text("UPDATE auth_users SET role = 'admin' WHERE id = 1"))
        with op.batch_alter_table("auth_users") as batch:
            batch.alter_column("role", server_default=None)

    if "strategies" in tables and "owner_user_id" not in columns("strategies"):
        with op.batch_alter_table("strategies") as batch:
            batch.add_column(sa.Column("owner_user_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_strategies_owner_user_id_auth_users",
                "auth_users",
                ["owner_user_id"],
                ["id"],
                ondelete="CASCADE",
            )
            batch.create_index("ix_strategies_owner_user_id", ["owner_user_id"])
        op.execute(sa.text("UPDATE strategies SET owner_user_id = 1 WHERE is_template = 0"))

    for table in ("backtest_runs", "signals", "orders"):
        if table in tables:
            add_user_column(table)
    if "event_logs" in tables:
        add_user_column("event_logs", nullable=True)
        op.execute(sa.text("UPDATE event_logs SET user_id = 1 WHERE user_id IS NULL"))
    for table in ("risk_settings", "engine_state", "connection_config"):
        if table in tables:
            add_user_column(table, unique=True)

    if "watchlist" in tables and "user_id" not in columns("watchlist"):
        with op.batch_alter_table(
            "watchlist", naming_convention={"pk": "pk_%(table_name)s"}
        ) as batch:
            batch.add_column(sa.Column("user_id", sa.Integer(), nullable=False, server_default="1"))
            batch.drop_constraint("pk_watchlist", type_="primary")
            batch.create_primary_key("pk_watchlist", ["user_id", "symbol"])
            batch.create_foreign_key(
                "fk_watchlist_user_id_auth_users",
                "auth_users",
                ["user_id"],
                ["id"],
                ondelete="CASCADE",
            )
        with op.batch_alter_table("watchlist") as batch:
            batch.alter_column("user_id", server_default=None)


def downgrade() -> None:
    # Multi-user ownership is intentionally not destructively downgraded because
    # collapsing several brokerage accounts into one could submit wrong orders.
    raise RuntimeError("QuantPilot multi-user databases cannot be safely downgraded")
