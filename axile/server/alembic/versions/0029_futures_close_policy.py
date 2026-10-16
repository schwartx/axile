"""将旧算法的今昨顺序一次性迁移为账户品种平仓规则。"""

import json
import logging
import re

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None
# 固定为此迁移发布时的预设，避免未来预设变化影响历史迁移。
PRODUCTS = [
    "A",
    "AD",
    "AG",
    "AL",
    "AO",
    "AP",
    "AU",
    "B",
    "BB",
    "BC",
    "BR",
    "BU",
    "BZ",
    "C",
    "CF",
    "CJ",
    "CS",
    "CU",
    "CY",
    "EB",
    "EC",
    "EG",
    "FB",
    "FG",
    "FU",
    "HC",
    "I",
    "IC",
    "IF",
    "IH",
    "IM",
    "J",
    "JD",
    "JM",
    "JR",
    "L",
    "LC",
    "LG",
    "LH",
    "LR",
    "LU",
    "M",
    "MA",
    "NI",
    "NR",
    "OI",
    "OP",
    "P",
    "PB",
    "PD",
    "PF",
    "PG",
    "PK",
    "PL",
    "PM",
    "PP",
    "PR",
    "PS",
    "PT",
    "PX",
    "RB",
    "RI",
    "RM",
    "RR",
    "RS",
    "RU",
    "SA",
    "SC",
    "SF",
    "SH",
    "SI",
    "SM",
    "SN",
    "SP",
    "SR",
    "SS",
    "T",
    "TA",
    "TF",
    "TL",
    "TS",
    "UR",
    "V",
    "WH",
    "WR",
    "Y",
    "ZC",
    "ZN",
]
PRIORITIES = {"昨今": "yesterday_first", "今昨": "today_first"}


def migrate_execution(raw: dict, account_id: int, channel: str = "ctp") -> dict:
    """冲突时拒绝任意选择；历史执行记录不参与迁移。"""
    result = json.loads(json.dumps(raw))
    if channel not in {"ctp", "tq"}:
        result.setdefault("futures_close_policy", {"mode": "rules", "fallback": "yesterday_first", "overrides": {}})
        return result
    previous = []
    for field in ("algorithm", "empty_positions_algorithm"):
        params = (result.get(field) or {}).get("params") or {}
        value = params.pop("offset_priority", None)
        if value is not None:
            if value not in PRIORITIES:
                raise ValueError(f"账户 {account_id}: 旧平仓顺序无效，请先处理")
            previous.append(PRIORITIES[value])
    if len(set(previous)) > 1:
        raise ValueError(f"账户 {account_id}: 主交易和清仓顺序冲突，请先统一")
    fallback = previous[0] if previous else "yesterday_first"
    # 原来的全局先今后昨要覆盖本版预设，避免迁移后默默改变已有选择。
    overrides = dict.fromkeys(PRODUCTS, fallback) if fallback == "today_first" else {}
    explicit = {}
    for symbol, rule in (result.get("trade_rules") or {}).items():
        if not isinstance(rule, dict):
            continue
        old = rule.pop("offset_priority", None)
        if old is None:
            continue
        match = re.fullmatch(r"([A-Za-z]+)\d{3,4}", symbol.rsplit(".", 1)[-1])
        if old not in PRIORITIES or match is None:
            raise ValueError(f"账户 {account_id}: 合约平仓规则无法迁移，请先处理")
        product, priority = match[1].upper(), PRIORITIES[old]
        if product in explicit and explicit[product] != priority:
            raise ValueError(f"账户 {account_id}: {product} 不同月份的平仓规则冲突，请先统一")
        explicit[product] = priority
    overrides.update(explicit)
    result.setdefault("futures_close_policy", {"mode": "rules", "fallback": fallback, "overrides": overrides})
    return result


def _settings_table():
    return sa.table("account_settings", sa.column("account_id"), sa.column("execution", sa.JSON()))


def _backup_table():
    return sa.table(
        "_futures_close_policy_backup",
        sa.column("account_id"),
        sa.column("previous_execution", sa.JSON()),
        sa.column("migrated_execution", sa.JSON()),
    )


def upgrade() -> None:
    """先检查所有账户，再保存迁移前后快照；不修改历史执行证据。"""
    connection = op.get_bind()
    table = _settings_table()
    accounts = sa.table("account", sa.column("id"), sa.column("trade_channel"))
    rows = connection.execute(
        sa.select(table.c.account_id, table.c.execution, accounts.c.trade_channel).join(
            accounts, accounts.c.id == table.c.account_id
        )
    )
    pending = [
        (row.account_id, row.execution, migrate_execution(row.execution, row.account_id, row.trade_channel))
        for row in rows
    ]
    op.create_table(
        "_futures_close_policy_backup",
        sa.Column("account_id", sa.Integer(), primary_key=True),
        sa.Column("previous_execution", sa.JSON(), nullable=False),
        sa.Column("migrated_execution", sa.JSON(), nullable=False),
    )
    backup = _backup_table()
    for account_id, previous, execution in pending:
        connection.execute(
            backup.insert().values(account_id=account_id, previous_execution=previous, migrated_execution=execution)
        )
        connection.execute(table.update().where(table.c.account_id == account_id).values(execution=execution))
        logging.getLogger(__name__).info(
            "账户 %s: 平仓规则迁移完成，%s 个品种覆盖", account_id, len(execution["futures_close_policy"]["overrides"])
        )


def _previous_execution(account_id, execution, backups):
    archived = backups.get(account_id)
    if archived is not None and execution == archived["migrated_execution"]:
        return archived["previous_execution"]
    # 升级后新建但尚未修改规则的账户仍能安全降级。
    if archived is None and execution.get("futures_close_policy") == {
        "mode": "rules",
        "fallback": "yesterday_first",
        "overrides": {},
    }:
        return {k: v for k, v in execution.items() if k != "futures_close_policy"}
    raise ValueError(f"账户 {account_id}: 升级后执行设置已修改，不能无损降级，请使用备份恢复")


def downgrade() -> None:
    """仅在未产生无法表达的新设置时恢复旧配置，全部检查通过后再写入。"""
    connection = op.get_bind()
    table, backup = _settings_table(), _backup_table()
    backups = {row["account_id"]: row for row in connection.execute(sa.select(backup)).mappings()}
    pending = [
        (row.account_id, _previous_execution(row.account_id, row.execution, backups))
        for row in connection.execute(sa.select(table))
    ]
    for account_id, execution in pending:
        connection.execute(table.update().where(table.c.account_id == account_id).values(execution=execution))
    op.drop_table("_futures_close_policy_backup")
