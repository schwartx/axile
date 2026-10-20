"""Version-owned execution summaries for small analysis reads."""

import sqlalchemy as sa
from sqlmodel import SQLModel

metadata = SQLModel.metadata
execution_summary = sa.Table(
    "performance_execution_summary",
    metadata,
    sa.Column("snapshot_id", sa.Text, sa.ForeignKey("performance_snapshot.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("record_id", sa.Integer, primary_key=True),
    sa.Column("time", sa.Float, nullable=False),
    *[sa.Column(name, sa.Float) for name in ("value", "cost", "loss_bp", "coverage")],
    *[sa.Column(name, sa.Integer, nullable=False) for name in ("covered", "count", "fee_covered")],
    sa.Column("amount_complete", sa.Boolean, nullable=False),
    sa.Column("covered_value", sa.Float, nullable=False),
    sa.Column("estimated", sa.Integer, nullable=False),
    sa.Column("symbol_count", sa.Integer),
    sa.Column("has_transactions", sa.Boolean, nullable=False),
    sa.Index("ix_performance_execution_summary_time", "snapshot_id", "time", "record_id"),
)
execution_fee = sa.Table(
    "performance_execution_fee",
    metadata,
    sa.Column("snapshot_id", sa.Text, sa.ForeignKey("performance_snapshot.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("record_id", sa.Integer, primary_key=True),
    sa.Column("currency", sa.Text, primary_key=True),
    sa.Column("value", sa.Float, nullable=False),
    sa.Column("count", sa.Integer, nullable=False),
)

READ_VIEW_TABLES = (execution_summary, execution_fee)
