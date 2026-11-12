"""当前会话由存活实例提供，数据库保留访问历史及主动断开意愿。"""

from typing import Literal

from pydantic import Field

from axile.server.db.models.base import Model

type AccountSessionStatus = Literal[
    "uninitialized", "connecting", "ready", "recovering", "unavailable", "disconnected", "unknown"
]


class AccountConnectionState(Model):
    """保存最近访问和故障；配置签名、实例和序号用于拒绝旧观察。"""

    account_id: int
    config_key: str
    instance_id: str = Field(default="")
    sequence: int = Field(default=0)
    status: str = Field(default="unknown")
    observed_at: str | None = None
    message: str | None = None
    error: dict[str, object] | None = Field(default=None)
    label: str | None = Field(default=None)
    presentation: dict[str, object] | None = Field(default=None)
    last_success_at: str | None = None
    last_failure: dict[str, object] | None = Field(default=None)
    manually_disconnected: bool = False
