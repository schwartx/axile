"""系统时间诊断的配置与最近一次检查证据。"""

from typing import Any

from pydantic import Field

from axile.server.db.models.base import Model


class TimeCheckSettingsRow(Model):
    """独立于账户的单例配置，版本用于并发编辑校验。"""

    id: int = Field(default=1)
    version: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


class TimeCheckStateRow(Model):
    """持久化最近结果及尚未恢复的异常，不累积检查历史。"""

    id: int = Field(default=1)
    data: dict[str, Any] = Field(default_factory=dict)
