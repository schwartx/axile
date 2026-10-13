"""记录流控会话交接和异常退出边界。"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """添加监控会话表，不改变现有计数表。"""
    op.create_table(
        "account_control_session",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.String(), nullable=False, unique=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
        sa.Column("execution_id", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("started_at_ms", sa.Integer(), nullable=False),
        sa.Column("finished_at_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
    )
    op.create_index("ix_account_control_session_account_id", "account_control_session", ["account_id"])


def downgrade() -> None:
    """仅移除监控会话表。"""
    op.drop_table("account_control_session")
