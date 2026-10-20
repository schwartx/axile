"""执行记录同事务写入的结构化分析事实，供 Rust 直接读取。"""

import sqlalchemy as sa
from sqlmodel import SQLModel

metadata = SQLModel.metadata

# 不可投影的记录也保留版本和错误，避免每次分析都重新解析历史载荷。
source_execution = sa.Table(
    "analysis_source_execution",
    metadata,
    sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("account_id", sa.Integer, sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
    sa.Column("projection_version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("projection_error", sa.Text),
    sa.Column("event_tag", sa.Text),
    sa.Column("event_text", sa.Text),
    sa.Column("time", sa.Float),
    sa.Column("success", sa.Boolean, nullable=False, server_default="0"),
    sa.Column("noop", sa.Boolean, nullable=False, server_default="0"),
    sa.Column("key", sa.Text),
    sa.Column("execution_id", sa.Text),
    sa.Column("created_at", sa.Text),
    sa.Column("is_success", sa.Integer),
    sa.Column("duration_sec", sa.Float),
    sa.Column("trade_count", sa.Integer),
    sa.Column("symbol_count", sa.Integer),
    sa.Column("position_count", sa.Integer),
    sa.Column("positions_present", sa.Boolean, nullable=False, server_default="0"),
    sa.Column("reason", sa.Text),
    sa.Column("raw_present", sa.Integer, nullable=False, server_default="0"),
    sa.Column("has_symbol_results", sa.Boolean, nullable=False, server_default="0"),
    *[
        sa.Column("raw_" + field, sa.Text)
        for field in (
            "status",
            "task_status",
            "error",
            "outcome",
            "outcome_reason",
            "reason_code",
            "msg",
            "execution_kind",
        )
    ],
    sa.Index("ix_analysis_source_execution_account_time", "account_id", "time", "record_id"),
)


def _source_child(name: str, keys: list[sa.Column], values: list[sa.Column]) -> sa.Table:
    """通过执行事实主键约束子行的生命周期。"""
    return sa.Table(
        name,
        metadata,
        sa.Column(
            "record_id",
            sa.Integer,
            sa.ForeignKey("analysis_source_execution.record_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        *keys,
        *values,
    )


source_trade = _source_child(
    "analysis_source_trade",
    [sa.Column("trade_index", sa.Integer, primary_key=True)],
    [
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("time", sa.Float, nullable=False),
        sa.Column("day", sa.Text, nullable=False),
        sa.Column("value", sa.Float),
        sa.Column("cost", sa.Float),
        sa.Column("loss_bp", sa.Float),
        sa.Column("fee", sa.Float),
        sa.Column("fee_currency", sa.Text),
        sa.Column("estimated", sa.Boolean, nullable=False),
        sa.Column("side", sa.Text, nullable=False, server_default="none"),
        sa.Column("quantity", sa.Float),
        sa.Column("price", sa.Float),
        sa.Column("reference", sa.Float),
        sa.Column("reference_source", sa.Text),
    ],
)
source_execution_symbol = _source_child(
    "analysis_source_execution_symbol",
    [sa.Column("symbol", sa.Text, primary_key=True)],
    [sa.Column("present", sa.Integer, nullable=False)]
    + [sa.Column(field, sa.Text) for field in ("status", "error", "outcome", "outcome_reason", "reason_code")],
)
source_position = _source_child(
    "analysis_source_position",
    [sa.Column("idx", sa.Integer, primary_key=True)],
    [sa.Column("symbol", sa.Text), sa.Column("quantity", sa.Float), sa.Column("direction", sa.Text)],
)
source_attempt = _source_child(
    "analysis_source_attempt",
    [sa.Column("idx", sa.Integer, primary_key=True)],
    [
        sa.Column("symbol", sa.Text),
        sa.Column("planned", sa.Float),
        sa.Column("filled", sa.Float),
        sa.Column("reason", sa.Text),
    ],
)
source_transaction = _source_child(
    "analysis_source_transaction",
    [sa.Column("idx", sa.Integer, primary_key=True)],
    [sa.Column("symbol", sa.Text), sa.Column("side", sa.Text)]
    + [sa.Column(field, sa.Float) for field in ("quantity", "price", "reference")]
    + [sa.Column("reference_source", sa.Text)]
    + [sa.Column(field, sa.Float) for field in ("time", "end_time")]
    + [sa.Column("time_estimated", sa.Boolean, nullable=False)]
    + [sa.Column(field, sa.Float) for field in ("before", "target", "value", "cost", "loss_bp", "coverage")]
    + [sa.Column("covered", sa.Integer), sa.Column("count", sa.Integer)]
    + [sa.Column("amount_complete", sa.Boolean, nullable=False), sa.Column("fee_covered", sa.Integer)],
)
source_transaction_fee = _source_child(
    "analysis_source_transaction_fee",
    [sa.Column("transaction_idx", sa.Integer, primary_key=True), sa.Column("currency", sa.Text, primary_key=True)],
    [sa.Column("value", sa.Float, nullable=False)],
)

# 显式清理不依赖连接是否启用外键，先删除子行再删除执行事实。
SOURCE_FACT_TABLES = (
    source_transaction_fee,
    source_transaction,
    source_attempt,
    source_position,
    source_execution_symbol,
    source_trade,
    source_execution,
)
