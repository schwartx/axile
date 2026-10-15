"""当前会话由存活实例提供，数据库保留访问历史及主动断开意愿。"""

from typing import ClassVar, Literal

from pydantic import field_validator
from sqlalchemy import JSON, Column, ForeignKey, Integer, Text
from sqlmodel import Field, SQLModel

from axile.executor.connection_presentation import ConnectionPresentation, presentation_snapshot

type AccountSessionStatus = Literal[
    "uninitialized", "connecting", "ready", "recovering", "unavailable", "disconnected", "unknown"
]


class AccountConnectionFailure(SQLModel):
    """最近一次连接故障，恢复后仍作为历史证据保留。"""

    observed_at: str
    message: str | None = None
    label: str | None = None
    error: dict[str, object] | None = None
    presentation: ConnectionPresentation | None = None


class AccountConnectionPublic(SQLModel):
    """账户读取接口的轻量连通状态。"""

    status: AccountSessionStatus = "uninitialized"
    observed_at: str | None = None
    message: str | None = None
    label: str | None = None
    worker_alive: bool = False
    sampled_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    manually_disconnected: bool = False
    retry_after_seconds: int = 0


class AccountConnectionDetail(AccountConnectionPublic):
    """当前故障详情及独立的历史故障；恢复后仍保留历史。"""

    error: dict[str, object] | None = None
    presentation: ConnectionPresentation | None = None
    last_failure: AccountConnectionFailure | None = None

    @field_validator("presentation", mode="before")
    @classmethod
    def _usable_presentation(cls, value: object) -> object:
        """损坏或旧的展示内容降级，不妨碍读取原始故障。"""
        return presentation_snapshot(value)


class AccountConnectionState(SQLModel, table=True):
    """保存最近访问和故障；配置签名、实例和序号用于拒绝旧观察。"""

    __tablename__: ClassVar[str] = "account_connection_state"
    account_id: int = Field(sa_column=Column(Integer, ForeignKey("account.id", ondelete="CASCADE"), primary_key=True))
    config_key: str = Field(sa_column=Column(Text, nullable=False))
    instance_id: str = Field(default="", sa_column=Column(Text, nullable=False))
    sequence: int = Field(default=0)
    status: str = Field(default="unknown")
    observed_at: str | None = None
    message: str | None = None
    error: dict[str, object] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    label: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    presentation: dict[str, object] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    last_success_at: str | None = None
    last_failure: dict[str, object] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    manually_disconnected: bool = False
