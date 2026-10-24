"""冻结 0034 的测试侧 DML 表声明；建库始终使用真实 Rust 迁移。"""

import json
from pathlib import Path

import sqlalchemy as sa

_COLUMNS = json.loads(Path(__file__).with_name("public-0034.json").read_text(encoding="utf-8"))["table_columns"]
_METADATA = sa.MetaData()
_TYPES = {
    "TEXT": sa.Text,
    "INTEGER": sa.Integer,
    "BIGINT": sa.BigInteger,
    "FLOAT": sa.Float,
    "REAL": sa.REAL,
    "BOOLEAN": sa.Boolean,
    "JSON": sa.JSON,
    "VARCHAR": sa.String,
    "DATE": sa.Date,
}


def table(name: str) -> sa.Table:
    """只声明查询和夹具写入需要的类型，不替代迁移中的约束或触发器。"""
    if name not in _METADATA.tables:
        columns = []
        for column in _COLUMNS[name]:
            sql_type = column["type"]
            kind = _TYPES.get(sql_type)
            if kind is None:
                assert sql_type.startswith("VARCHAR(") and sql_type.endswith(")"), sql_type
                value_type = sa.String(int(sql_type[8:-1]))
            else:
                value_type = kind()
            columns.append(
                sa.Column(
                    column["name"],
                    value_type,
                    nullable=column["nullable"],
                    primary_key=column["primary_key"],
                    server_default=sa.text(column["default"]) if column["default"] is not None else None,
                )
            )
        sa.Table(name, _METADATA, *columns)
    return _METADATA.tables[name]


analysis_state = table("account_analysis")
analysis_snapshot = table("performance_snapshot")
analysis_calendar_day = table("analysis_calendar_day")
cost_execution = table("performance_cost_execution")
cost_trade = table("performance_cost_trade")
cost_attempt = table("performance_cost_attempt")
cost_execution_symbol = table("performance_cost_execution_symbol")
cost_position = table("performance_cost_position")
cost_transaction = table("performance_cost_transaction")
cost_transaction_fee = table("performance_cost_transaction_fee")
result_binding = table("performance_result_binding")
result_calendar_range = table("performance_result_calendar_range")
result_daily_cost = table("performance_result_daily_cost")
result_daily_fee = table("performance_result_daily_fee")
result_event = table("performance_result_event")
result_fallback = table("performance_result_fallback")
result_point = table("performance_result_point")
result_range = table("performance_result_range")
result_skip = table("performance_result_skip")
RESULT_TABLES = (
    result_daily_fee,
    result_daily_cost,
    result_event,
    result_calendar_range,
    result_binding,
    result_fallback,
    result_skip,
    result_point,
    result_range,
)
