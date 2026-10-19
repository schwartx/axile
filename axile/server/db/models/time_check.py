"""系统时间诊断的配置与最近一次检查证据。"""

from typing import Any, ClassVar

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class TimeCheckSettingsRow(SQLModel, table=True):
    """独立于账户的单例配置，版本用于并发编辑校验。"""

    __tablename__: ClassVar[str] = "time_check_settings"
    id: int = Field(default=1, primary_key=True)
    version: int = 0
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class TimeCheckStateRow(SQLModel, table=True):
    """持久化最近结果及尚未恢复的异常，不累积检查历史。"""

    __tablename__: ClassVar[str] = "time_check_state"
    id: int = Field(default=1, primary_key=True)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
