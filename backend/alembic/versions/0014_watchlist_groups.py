"""User-named watchlist groups with ordering.

Revision ID: 0014_watchlist_groups
Revises: 0013_market_data_source
"""

from alembic import op
import sqlalchemy as sa

revision = "0014_watchlist_groups"
down_revision = "0013_market_data_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "watchlist_groups" not in inspector.get_table_names():
        op.create_table(
            "watchlist_groups",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("name", sa.String(40), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
    columns = {c["name"] for c in sa.inspect(bind).get_columns("watchlist")}
    with op.batch_alter_table("watchlist") as batch:
        if "group_id" not in columns:
            batch.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
        if "position" not in columns:
            batch.add_column(sa.Column("position", sa.Integer(), nullable=False, server_default="0"))
    # Move every existing watchlist into one default group per user.
    users = [row[0] for row in bind.execute(sa.text("SELECT DISTINCT user_id FROM watchlist")).fetchall()]
    for user_id in users:
        existing = bind.execute(sa.text("SELECT id FROM watchlist_groups WHERE user_id=:u ORDER BY position, id LIMIT 1"), {"u": user_id}).scalar()
        if existing is None:
            bind.execute(sa.text("INSERT INTO watchlist_groups (user_id, name, position, created_at) VALUES (:u, '自选', 0, CURRENT_TIMESTAMP)"), {"u": user_id})
            existing = bind.execute(sa.text("SELECT id FROM watchlist_groups WHERE user_id=:u ORDER BY id LIMIT 1"), {"u": user_id}).scalar()
        symbols = [row[0] for row in bind.execute(sa.text("SELECT symbol FROM watchlist WHERE user_id=:u AND group_id IS NULL ORDER BY symbol"), {"u": user_id}).fetchall()]
        for position, symbol in enumerate(symbols):
            bind.execute(sa.text("UPDATE watchlist SET group_id=:g, position=:p WHERE user_id=:u AND symbol=:s"),
                         {"g": existing, "p": position, "u": user_id, "s": symbol})


def downgrade() -> None:
    with op.batch_alter_table("watchlist") as batch:
        batch.drop_column("position")
        batch.drop_column("group_id")
    op.drop_table("watchlist_groups")
