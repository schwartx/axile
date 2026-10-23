"""Version-owned execution summaries for small analysis reads."""

import sqlalchemy as sa
from sqlmodel import SQLModel

metadata = SQLModel.metadata
execution_fee = sa.Table(
    "performance_execution_fee",
    metadata,
    sa.Column("snapshot_id", sa.Text, sa.ForeignKey("performance_snapshot.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("record_id", sa.Integer, primary_key=True),
    sa.Column("currency", sa.Text, primary_key=True),
    sa.Column("value", sa.Float, nullable=False),
    sa.Column("count", sa.Integer, nullable=False),
)

READ_VIEW_TABLES = (execution_fee,)
