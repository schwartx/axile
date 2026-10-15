"""分离账户连接历史与当前会话，并保留主动断开意愿。"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """旧成功记录无法证明曾成功访问账户，保持访问时间未知。"""
    op.add_column("account_connection_state", sa.Column("last_success_at", sa.Text(), nullable=True))
    op.add_column("account_connection_state", sa.Column("last_failure", sa.JSON(), nullable=True))
    op.add_column(
        "account_connection_state",
        sa.Column("manually_disconnected", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    connection = op.get_bind()
    table = sa.table(
        "account_connection_state",
        sa.column("account_id"),
        sa.column("status"),
        sa.column("observed_at"),
        sa.column("message"),
        sa.column("label"),
        sa.column("error", sa.JSON()),
        sa.column("presentation", sa.JSON()),
        sa.column("last_failure", sa.JSON()),
        sa.column("manually_disconnected"),
    )
    for row in connection.execute(sa.select(table)).mappings():
        if row["status"] in {"error", "cooldown"} and row["observed_at"]:
            failure = {key: row[key] for key in ("observed_at", "message", "label", "error", "presentation")}
            connection.execute(
                table.update().where(table.c.account_id == row["account_id"]).values(last_failure=failure)
            )
    connection.execute(table.update().where(table.c.status == "disconnected").values(manually_disconnected=True))


def downgrade() -> None:
    """移除历史与连接意愿字段，原有观察字段保持原样。"""
    with op.batch_alter_table("account_connection_state") as batch:
        batch.drop_column("manually_disconnected")
        batch.drop_column("last_failure")
        batch.drop_column("last_success_at")
