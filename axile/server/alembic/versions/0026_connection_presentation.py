"""连接观察保存渠道标签和动态展示快照。"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """旧记录保持空展示数据，不推测历史故障。"""
    op.add_column("account_connection_state", sa.Column("label", sa.Text(), nullable=True))
    op.add_column("account_connection_state", sa.Column("presentation", sa.JSON(), nullable=True))


def downgrade() -> None:
    """仅移除新增展示字段。"""
    with op.batch_alter_table("account_connection_state") as batch:
        batch.drop_column("presentation")
        batch.drop_column("label")
