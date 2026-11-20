"""Per-user LLM endpoint for natural-language strategy drafts.

Revision ID: 0011_llm_settings
Revises: 0010_cash_sweep_controls
"""

from alembic import op
from sqlalchemy import inspect

from app.models import LlmSettings

revision = "0011_llm_settings"
down_revision = "0010_cash_sweep_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if LlmSettings.__tablename__ not in inspect(bind).get_table_names():
        LlmSettings.__table__.create(bind)


def downgrade() -> None:
    op.drop_table("llm_settings")
