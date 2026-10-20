"""供 Rust 只读组装绩效日历的渠道逐日标量事实。"""

import sqlalchemy as sa
from sqlmodel import SQLModel

analysis_calendar_day = sa.Table(
    "analysis_calendar_day",
    SQLModel.metadata,
    sa.Column("channel", sa.Text, primary_key=True),
    sa.Column("day", sa.Text, primary_key=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("calendar_id", sa.Text),
    sa.Column("label", sa.Text),
)
