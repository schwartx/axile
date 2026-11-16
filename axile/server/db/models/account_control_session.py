"""流控运行会话：仅用于交接及异常退出完整性提示。"""

from dataclasses import dataclass, field


@dataclass(kw_only=True)
class AccountControlSession:
    """开始与收尾各写一次，不是计数检查点。"""

    id: int | None = field(default=None)
    instance_id: str
    account_id: int
    execution_id: str
    context_kind: str = field(default="unknown")
    effective_context_kind: str = field(default="unknown", metadata={"exclude": True})
    source: str = field(default="unknown")
    timezone: str
    started_at_ms: int
    finished_at_ms: int | None = None
    status: str = "active"
