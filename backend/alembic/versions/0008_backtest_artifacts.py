"""Persist compressed, reproducible backtest input bundles."""

from alembic import op
import sqlalchemy as sa

revision = "0008_backtest_artifacts"
down_revision = "0007_cash_sweep"
branch_labels = None
depends_on = None


def upgrade():
    if "backtest_artifacts" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "backtest_artifacts",
            sa.Column(
                "run_id",
                sa.String(36),
                sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("content", sa.LargeBinary(), nullable=False),
        )


def downgrade():
    op.drop_table("backtest_artifacts")
