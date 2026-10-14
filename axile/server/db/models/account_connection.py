"""账户连通性仅保留最新结果，恢复后不留旧错误。"""

from typing import ClassVar, Literal

from pydantic import field_validator
from sqlalchemy import JSON, Column, ForeignKey, Integer, Text
from sqlmodel import Field, SQLModel

from axile.executor.connection_presentation import ConnectionPresentation, presentation_snapshot


class AccountConnectionPublic(SQLModel):
    """账户读取接口的轻量连通状态。"""

    status: Literal["unknown", "ok", "error"] = "unknown"
    observed_at: str | None = None
    message: str | None = None
    label: str | None = None


class AccountConnectionDetail(AccountConnectionPublic):
    """最新异常的完整详情；正常或未知时为空。"""

    error: dict[str, object] | None = None
    presentation: ConnectionPresentation | None = None

    @field_validator("presentation", mode="before")
    @classmethod
    def _usable_presentation(cls, value: object) -> object:
        """损坏或旧的展示内容降级，不妨碍读取原始故障。"""
        return presentation_snapshot(value)


class AccountConnectionState(SQLModel, table=True):
    """配置签名、实例和序号用于拒绝旧观察，不构成历史记录。"""

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
