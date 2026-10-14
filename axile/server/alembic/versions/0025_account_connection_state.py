"""账户连通状态只保存每账户最新结果。"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增独立状态表，不从历史资产推断正常。"""
    op.create_table(
        "account_connection_state",
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("config_key", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """删除最新状态表。"""
    op.drop_table("account_connection_state")
