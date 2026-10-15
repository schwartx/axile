"""区分交易执行与账户操作的控制上下文，保留历史身份及计数。"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_TABLES = ("account_control_event", "account_control_counter_delta", "account_control_session")
_EXECUTIONS = ("execution_intent", "executerecord", "execution_event")


def upgrade() -> None:
    """已知交易优先，保留生成标识可识别的操作，其余历史类型保持未知。"""
    for table in _TABLES:
        for column in ("context_kind", "source"):
            op.add_column(table, sa.Column(column, sa.Text(), nullable=False, server_default="unknown"))
        real_execution = " OR ".join(
            f"EXISTS (SELECT 1 FROM {model} e WHERE e.account_id = {table}.account_id "
            f"AND e.execution_id = {table}.execution_id)"
            for model in _EXECUTIONS
        )
        # 附件没有账户字段；宁可保留为执行，也不能因保留前缀丢弃业务材料。
        real_execution += f" OR EXISTS (SELECT 1 FROM execution_artifact e WHERE e.execution_id = {table}.execution_id)"
        op.execute(sa.text(f"UPDATE {table} SET context_kind = 'execution' WHERE {real_execution}"))
        for prefix, source in (("orders-", "current_orders"), ("catalog-", "instrument_catalog")):
            offset = len(prefix) + 1
            op.execute(
                sa.text(
                    f"UPDATE {table} SET context_kind = 'account_operation', source = :source "
                    f"WHERE context_kind = 'unknown' AND substr(execution_id, 1, {len(prefix)}) = :prefix "
                    f"AND length(substr(execution_id, {offset})) = 32 "
                    f"AND substr(execution_id, {offset}) NOT GLOB '*[^0-9a-f]*'"
                ).bindparams(source=source, prefix=prefix)
            )


def downgrade() -> None:
    """仅移除分类字段，原有审计事实和计数增量不变。"""
    for table in reversed(_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("source")
            batch.drop_column("context_kind")
