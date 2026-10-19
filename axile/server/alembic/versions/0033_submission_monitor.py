"""原生报撤调用监测配置及预警事实；默认关闭，不修改交易额度。"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加独立监测表，不回填历史预警。"""
    op.create_table(
        "order_submission_monitor",
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_table(
        "order_submission_warning",
        sa.Column("event_uid", sa.Text(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
        sa.Column("occurred_at_ms", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_submission_warning_account_time", "order_submission_warning", ["account_id", "occurred_at_ms"])


def downgrade() -> None:
    """仅移除监测表。"""
    op.drop_table("order_submission_warning")
    op.drop_table("order_submission_monitor")
