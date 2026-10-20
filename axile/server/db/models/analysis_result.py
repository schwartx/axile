"""Normalized performance results; publication belongs to the Python transaction."""

import sqlalchemy as sa
from sqlmodel import SQLModel

metadata = SQLModel.metadata


def _result_table(name: str, keys: list[sa.Column], values: list[sa.Column]) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(
            "snapshot_id", sa.Text, sa.ForeignKey("performance_snapshot.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("range_key", sa.Text, primary_key=True),
        *keys,
        *values,
    )


result_range = _result_table(
    "performance_result_range",
    [],
    [
        sa.Column("backtest_included", sa.Boolean, nullable=False),
        sa.Column("engine_version", sa.Text, nullable=False),
        sa.Column("baseline", sa.Text),
        sa.Column("end", sa.Text),
    ]
    + [
        sa.Column(name, sa.Integer, nullable=False)
        for name in (
            "record_count",
            "observation_count",
            "used_record_count",
            "target_used_record_count",
            "invalid_asset_count",
        )
    ]
    + [
        sa.Column("sizing_fallback_count", sa.Integer),
        sa.Column("calendar_status", sa.Text, nullable=False),
        sa.Column("calendar_id", sa.Text),
        sa.Column("calendar_label", sa.Text),
        sa.Column("event_count", sa.Integer, nullable=False),
    ],
)
result_point = _result_table(
    "performance_result_point",
    [sa.Column("idx", sa.Integer, primary_key=True)],
    [
        sa.Column("date", sa.Text, nullable=False),
        sa.Column("observed_at", sa.Text, nullable=False),
        sa.Column("record_id", sa.Integer),
        sa.Column("execution_id", sa.Text),
    ]
    + [
        sa.Column(name, sa.Float)
        for name in (
            "account_return",
            "account_equity",
            "portfolio_return",
            "account_daily_return",
            "portfolio_daily_return",
            "target_portfolio_return",
            "target_portfolio_daily_return",
            "difference",
        )
    ]
    + [sa.Column("sizing_fallback", sa.Boolean, nullable=False)],
)
result_skip = _result_table(
    "performance_result_skip",
    [sa.Column("kind", sa.Text, primary_key=True)],
    [sa.Column(name, sa.Integer, nullable=False) for name in ("count", "missing_target", "missing_ticks")]
    + [sa.Column("first_time", sa.Text), sa.Column("last_time", sa.Text)],
)
result_fallback = _result_table(
    "performance_result_fallback",
    [sa.Column("idx", sa.Integer, primary_key=True)],
    [
        sa.Column("start", sa.Text, nullable=False),
        sa.Column("end", sa.Text, nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
    ],
)
result_binding = _result_table(
    "performance_result_binding",
    [sa.Column("idx", sa.Integer, primary_key=True)],
    [sa.Column("time", sa.Text, nullable=False), sa.Column("portfolio_id", sa.Integer)],
)
result_calendar_range = _result_table(
    "performance_result_calendar_range",
    [sa.Column("kind", sa.Text, primary_key=True), sa.Column("idx", sa.Integer, primary_key=True)],
    [sa.Column("start", sa.Text, nullable=False), sa.Column("end", sa.Text, nullable=False)],
)
result_event = _result_table(
    "performance_result_event",
    [sa.Column("idx", sa.Integer, primary_key=True)],
    [
        sa.Column("time", sa.Text, nullable=False),
        sa.Column("tag", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("execution_id", sa.Text),
    ],
)
result_daily_cost = _result_table(
    "performance_result_daily_cost",
    [sa.Column("day", sa.Text, primary_key=True)],
    [sa.Column(name, sa.Float) for name in ("value", "cost", "loss_bp", "coverage", "covered_value")]
    + [sa.Column(name, sa.Integer) for name in ("covered", "count", "fee_covered", "estimated")]
    + [sa.Column("amount_complete", sa.Boolean, nullable=False), sa.Column("present", sa.Integer, nullable=False)],
)
result_daily_fee = _result_table(
    "performance_result_daily_fee",
    [sa.Column("day", sa.Text, primary_key=True), sa.Column("currency", sa.Text, primary_key=True)],
    [sa.Column("value", sa.Float, nullable=False)],
)

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
