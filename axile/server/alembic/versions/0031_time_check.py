"""新增系统时间检查配置与证据。"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    """旧安装不插入首次检查待办。"""
    op.create_table(
        "time_check_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_table(
        "time_check_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
    )


def downgrade():
    """移除本功能独立的数据表。"""
    op.drop_table("time_check_state")
    op.drop_table("time_check_settings")
