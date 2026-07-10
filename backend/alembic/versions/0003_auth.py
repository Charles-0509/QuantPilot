"""Add single-admin authentication and opaque OAuth2 sessions.

Revision ID: 0003_auth
Revises: 0002_connection_config
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_auth"
down_revision = "0002_connection_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "auth_users" not in tables:
        op.create_table(
            "auth_users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("username", sa.String(length=64), nullable=False),
            sa.Column("username_normalized", sa.String(length=64), nullable=False),
            sa.Column("password_hash", sa.Text(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("failed_login_count", sa.Integer(), nullable=False),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("username_normalized"),
        )
        op.create_index("ix_auth_users_username_normalized", "auth_users", ["username_normalized"], unique=True)
    if "oauth_access_tokens" not in tables:
        op.create_table(
            "oauth_access_tokens",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["auth_users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash"),
        )
        op.create_index("ix_oauth_access_tokens_user_id", "oauth_access_tokens", ["user_id"])
        op.create_index("ix_oauth_access_tokens_token_hash", "oauth_access_tokens", ["token_hash"], unique=True)
        op.create_index("ix_oauth_access_tokens_expires_at", "oauth_access_tokens", ["expires_at"])
        op.create_index("ix_oauth_access_tokens_revoked_at", "oauth_access_tokens", ["revoked_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "oauth_access_tokens" in tables:
        op.drop_table("oauth_access_tokens")
    if "auth_users" in tables:
        op.drop_table("auth_users")
