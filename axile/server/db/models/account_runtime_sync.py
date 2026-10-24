"""账户持久化配置到进程运行态的对齐记录。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from axile.server.db.models.base import Model, now_str


class AccountRuntimeSync(Model):
    """每个账户一条的运行态对齐目标与最近状态。

    ``pending`` 包含柜台暂未初始化；``last_error`` 保存最近一次未完成对齐的
    可读原因，不代表该目标必为 failed。仅完整对齐成功才清除原因。
    """

    id: int | None = Field(default=None)
    account_id: int
    revision: int = Field(default=1)
    status: Literal["pending", "synchronized", "failed"] = Field(default="pending")
    reset_worker: bool = Field(default=False)
    create_request_key: str | None = Field(default=None)
    attempts: int = Field(default=0)
    last_error: str | None = Field(default=None)
    requested_at: str = Field(default_factory=now_str)
    last_attempt_at: str | None = Field(default=None)
    synchronized_at: str | None = Field(default=None)


class AccountRuntimeSyncPublic(Model):
    """客户端可见的运行态同步状态。"""

    status: Literal["pending", "synchronized", "failed"]
    revision: int
    attempts: int
    last_error: str | None = None
    last_attempt_at: str | None = None
    synchronized_at: str | None = None
