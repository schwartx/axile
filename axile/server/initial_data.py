"""从已持久化账户恢复运行态的启动辅助函数."""

from axile.server.core.scheduler import scheduler
from axile.server.execution.account_runtime_sync import recover_account_runtime_on_startup


async def init_scheduler() -> None:
    """在后台从数据库真源并发重建账户运行态。"""
    # scheduler 与 worker 都是进程内存态，不能由上个进程的 synchronized 状态短路。
    await recover_account_runtime_on_startup(scheduler)
