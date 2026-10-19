"""原生报单流水，与额度计数独立持久化，不从历史放行记录推测发送。"""

from typing import Any, ClassVar

from sqlalchemy import JSON, Column, ForeignKey, Index, Integer, Text
from sqlmodel import Field, SQLModel


class OrderSubmission(SQLModel, table=True):
    """以调用唯一身份去重，并保留最新版本的同步及异步结果。"""

    __tablename__: ClassVar[str] = "order_submission"
    __table_args__ = (Index("ix_order_submission_account_time", "account_id", "occurred_at_ms"),)

    submission_uid: str = Field(sa_column=Column(Text, primary_key=True))
    account_id: int = Field(sa_column=Column(Integer, ForeignKey("account.id", ondelete="CASCADE"), nullable=False))
    occurred_at_ms: int = Field(sa_column=Column(Integer, nullable=False))
    revision: int = Field(sa_column=Column(Integer, nullable=False))
    data: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))


class OrderSubmissionMonitor(SQLModel, table=True):
    """账户原生调用监测阈值；不参与风控额度放行。"""

    __tablename__: ClassVar[str] = "order_submission_monitor"

    account_id: int = Field(sa_column=Column(Integer, ForeignKey("account.id", ondelete="CASCADE"), primary_key=True))
    data: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))


class OrderSubmissionWarning(SQLModel, table=True):
    """阈值当笔的不可变预警；同配置版本及自然日仅产生一次。"""

    __tablename__: ClassVar[str] = "order_submission_warning"
    __table_args__ = (Index("ix_submission_warning_account_time", "account_id", "occurred_at_ms"),)

    event_uid: str = Field(sa_column=Column(Text, primary_key=True))
    account_id: int = Field(sa_column=Column(Integer, ForeignKey("account.id", ondelete="CASCADE"), nullable=False))
    occurred_at_ms: int = Field(sa_column=Column(Integer, nullable=False))
    data: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
