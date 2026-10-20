"""供 Rust 顺序读取的结构化收益回放事实。"""

import sqlalchemy as sa
from sqlmodel import SQLModel

metadata = SQLModel.metadata

# 事实与执行记录同事务写入；存在位保留缺失目标和显式清仓空映射的差异。
replay_observation = sa.Table(
    "replay_observation",
    metadata,
    sa.Column("record_id", sa.Integer, sa.ForeignKey("executerecord.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("account_id", sa.Integer, sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False),
    sa.Column("execution_id", sa.Text),
    sa.Column("time", sa.Text, nullable=False),
    sa.Column("asset", sa.Float),
    sa.Column("sizing_fallback", sa.Boolean, nullable=False),
    sa.Column("target_present", sa.Boolean, nullable=False),
    sa.Column("target_weight_present", sa.Boolean, nullable=False),
    sa.Column("target_units_present", sa.Boolean, nullable=False),
    sa.Column("target_quantities_present", sa.Boolean, nullable=False),
    sa.Index("ix_replay_observation_account_time", "account_id", "time", "record_id"),
)

replay_observation_symbol = sa.Table(
    "replay_observation_symbol",
    metadata,
    sa.Column(
        "record_id", sa.Integer, sa.ForeignKey("replay_observation.record_id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("symbol", sa.Text, primary_key=True),
    sa.Column("target", sa.Float),
    sa.Column("price", sa.Float),
    sa.Column("target_weight", sa.Float),
    sa.Column("target_units", sa.Float),
    sa.Column("target_quantity", sa.Float),
)
