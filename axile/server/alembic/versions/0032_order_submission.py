"""新增原生报单事实流水；历史额度计数不回填为发送记录。"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立账户时间索引及调用唯一键。"""
    op.create_table(
        "order_submission",
        sa.Column("submission_uid", sa.Text(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
        sa.Column("occurred_at_ms", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_order_submission_account_time", "order_submission", ["account_id", "occurred_at_ms"])


def downgrade() -> None:
    """只移除新增流水。"""
    op.drop_table("order_submission")
