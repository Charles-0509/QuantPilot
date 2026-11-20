"""LLM API flavor: OpenAI-compatible chat completions or Anthropic Messages.

Revision ID: 0012_llm_provider
Revises: 0011_llm_settings
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_llm_provider"
down_revision = "0011_llm_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("llm_settings")}
    if "provider" not in columns:
        op.add_column("llm_settings", sa.Column("provider", sa.String(16), nullable=False, server_default="openai"))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("llm_settings")}
    if "provider" in columns:
        op.drop_column("llm_settings", "provider")
