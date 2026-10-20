"""Persistent analysis read models; no execution payloads on the snapshot path."""

import sqlalchemy as sa
from sqlmodel import SQLModel

from axile.server.db.models.analysis_calendar import analysis_calendar_day as analysis_calendar_day
from axile.server.db.models.analysis_replay import replay_observation as replay_observation
from axile.server.db.models.analysis_replay import replay_observation_symbol as replay_observation_symbol
from axile.server.db.models.analysis_result import RESULT_TABLES as RESULT_TABLES
from axile.server.db.models.analysis_source import SOURCE_FACT_TABLES as SOURCE_FACT_TABLES
from axile.server.db.models.analysis_view import READ_VIEW_TABLES as READ_VIEW_TABLES

metadata = SQLModel.metadata
analysis_state = sa.Table(
    "account_analysis",
    metadata,
    sa.Column("account_id", sa.Integer, sa.ForeignKey("account.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("source_version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("published_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("current_snapshot", sa.Text),
    sa.Column("previous_snapshot", sa.Text),
    sa.Column("requested", sa.Boolean, nullable=False, server_default="0"),
    sa.Column("running_version", sa.Integer),
    sa.Column("running_token", sa.Text),
    sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
    sa.Column("retry_at", sa.Float, nullable=False, server_default="0"),
    sa.Column("error", sa.Text),
    sa.Column("logic_version", sa.Text),
    sa.Column("engine_version", sa.Text),
)
analysis_snapshot = sa.Table(
    "performance_snapshot",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("account_id", sa.Integer, sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True),
    sa.Column("source_version", sa.Integer, nullable=False),
    sa.Column("logic_version", sa.Text, nullable=False),
    sa.Column("engine_version", sa.Text, nullable=False),
    sa.Column("computed_at", sa.Text, nullable=False),
    sa.Column("data_until", sa.Text),
    sa.Column("settings", sa.JSON, nullable=False),
    sa.Column("ranges", sa.JSON, nullable=False),
    sa.Column("cost_schema_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("result_schema_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("read_view_schema_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("backtest_weight_type", sa.Text),
    sa.Column("backtest_fee_rate", sa.Float),
)
cost_execution = sa.Table(
    "performance_cost_execution",
    metadata,
    sa.Column("snapshot_id", sa.Text, sa.ForeignKey("performance_snapshot.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("record_id", sa.Integer, primary_key=True),
    sa.Column("time", sa.Float, nullable=False),
    sa.Column("success", sa.Boolean, nullable=False),
    sa.Column("noop", sa.Boolean, nullable=False),
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
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Index("ix_performance_execution_time", "snapshot_id", "time"),
)
cost_trade = sa.Table(
    "performance_cost_trade",
    metadata,
    sa.Column("snapshot_id", sa.Text, sa.ForeignKey("performance_snapshot.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("record_id", sa.Integer, nullable=False),
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
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Index("ix_performance_trade_record", "snapshot_id", "record_id", "time"),
    sa.Index("ix_performance_trade_symbol", "snapshot_id", "symbol", "time"),
)


def _cost_child(name: str, keys: list[sa.Column], values: list[sa.Column]) -> sa.Table:
    """Keep immutable cost facts owned by their published snapshot."""
    return sa.Table(
        name,
        metadata,
        sa.Column(
            "snapshot_id", sa.Text, sa.ForeignKey("performance_snapshot.id", ondelete="CASCADE"), primary_key=True
        ),
        *keys,
        *values,
    )


cost_range = _cost_child(
    "performance_cost_range",
    [sa.Column("range_key", sa.Text, primary_key=True)],
    [sa.Column("baseline", sa.Float), sa.Column("end", sa.Float)],
)
cost_execution_symbol = _cost_child(
    "performance_cost_execution_symbol",
    [sa.Column("record_id", sa.Integer, primary_key=True), sa.Column("symbol", sa.Text, primary_key=True)],
    [sa.Column("present", sa.Integer, nullable=False)]
    + [sa.Column(field, sa.Text) for field in ("status", "error", "outcome", "outcome_reason", "reason_code")],
)
cost_position = _cost_child(
    "performance_cost_position",
    [sa.Column("record_id", sa.Integer, primary_key=True), sa.Column("idx", sa.Integer, primary_key=True)],
    [sa.Column("symbol", sa.Text), sa.Column("quantity", sa.Float), sa.Column("direction", sa.Text)],
)
cost_attempt = _cost_child(
    "performance_cost_attempt",
    [sa.Column("record_id", sa.Integer, primary_key=True), sa.Column("idx", sa.Integer, primary_key=True)],
    [
        sa.Column("symbol", sa.Text),
        sa.Column("planned", sa.Float),
        sa.Column("filled", sa.Float),
        sa.Column("reason", sa.Text),
    ],
)
cost_transaction = _cost_child(
    "performance_cost_transaction",
    [sa.Column("record_id", sa.Integer, primary_key=True), sa.Column("idx", sa.Integer, primary_key=True)],
    [sa.Column("symbol", sa.Text), sa.Column("side", sa.Text)]
    + [sa.Column(field, sa.Float) for field in ("quantity", "price", "reference")]
    + [sa.Column("reference_source", sa.Text)]
    + [sa.Column(field, sa.Float) for field in ("time", "end_time")]
    + [sa.Column("time_estimated", sa.Boolean, nullable=False)]
    + [sa.Column(field, sa.Float) for field in ("before", "target", "value", "cost", "loss_bp", "coverage")]
    + [sa.Column("covered", sa.Integer), sa.Column("count", sa.Integer)]
    + [sa.Column("amount_complete", sa.Boolean, nullable=False), sa.Column("fee_covered", sa.Integer)],
)
cost_transaction_fee = _cost_child(
    "performance_cost_transaction_fee",
    [
        sa.Column("record_id", sa.Integer, primary_key=True),
        sa.Column("transaction_idx", sa.Integer, primary_key=True),
        sa.Column("currency", sa.Text, primary_key=True),
    ],
    [sa.Column("value", sa.Float, nullable=False)],
)
