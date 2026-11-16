"""当前会话由存活实例提供，数据库保留访问历史及主动断开意愿。"""

from dataclasses import dataclass, field
from typing import Literal

type AccountSessionStatus = Literal[
    "uninitialized", "connecting", "ready", "recovering", "unavailable", "disconnected", "unknown"
]


@dataclass(kw_only=True)
class AccountConnectionState:
    """保存最近访问和故障；配置签名、实例和序号用于拒绝旧观察。"""

    account_id: int
    config_key: str
    instance_id: str = field(default="")
    sequence: int = field(default=0)
    status: str = field(default="unknown")
    observed_at: str | None = None
    message: str | None = None
    error: dict[str, object] | None = field(default=None)
    label: str | None = field(default=None)
    presentation: dict[str, object] | None = field(default=None)
    last_success_at: str | None = None
    last_failure: dict[str, object] | None = field(default=None)
    manually_disconnected: bool = False
