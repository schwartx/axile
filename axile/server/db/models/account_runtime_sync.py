"""账户持久化配置到进程运行态的对齐记录。"""

from __future__ import annotations

from typing import Literal

from axile.server.db.models.base import Model


class AccountRuntimeSyncPublic(Model):
    """客户端可见的运行态同步状态。"""

    status: Literal["pending", "synchronized", "failed"]
    revision: int
    attempts: int
    last_error: str | None = None
    last_attempt_at: str | None = None
    synchronized_at: str | None = None
