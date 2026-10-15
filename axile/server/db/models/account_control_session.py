"""流控运行会话：仅用于交接及异常退出完整性提示。"""

from typing import ClassVar

from sqlalchemy import Column, ForeignKey, Integer, Text
from sqlmodel import Field, SQLModel


class AccountControlSession(SQLModel, table=True):
    """开始与收尾各写一次，不是计数检查点。"""

    __tablename__: ClassVar[str] = "account_control_session"
    id: int | None = Field(default=None, primary_key=True)
    instance_id: str = Field(unique=True)
    account_id: int = Field(
        sa_column=Column(Integer, ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    execution_id: str
    context_kind: str = Field(default="unknown", sa_column=Column(Text, nullable=False, server_default="unknown"))
    source: str = Field(default="unknown", sa_column=Column(Text, nullable=False, server_default="unknown"))
    timezone: str
    started_at_ms: int
    finished_at_ms: int | None = None
    status: str = "active"
