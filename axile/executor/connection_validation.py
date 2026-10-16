"""临时验证只管理生命周期，渠道连接步骤和账户核对与正式连接共用。"""

from collections.abc import Iterator
from contextlib import contextmanager

from axile.channels import get_channel
from axile.channels.contracts import ConnectionProgress
from axile.executor.connection_progress import (
    ConnectionProgressTracker,
    connection_progress_scope,
    connection_step_error,
)
from axile.executor.connection_reconciliation import reconcile_account_connection
from axile.executor.connection_state import connection_monitor
from axile.executor.models.unified_input_accounts import BaseAccountConfig


@contextmanager
def validation_progress(channel: str, report: ConnectionProgress) -> Iterator[None]:
    """把共用过程快照适配为验证任务事件，保留完整表格的按需读取。"""
    previous: dict[str, dict] = {}

    def observe(data: dict) -> None:
        for step in data["steps"]:
            if step["status"] not in {"running", "passed"} or previous.get(step["id"]) == step:
                continue
            previous[step["id"]] = step
            details = data.get("tables", {}).get(step["id"], step.get("details"))
            report(step["id"], step["status"], details)

    tracker = ConnectionProgressTracker(channel, "validation", observe)
    with connection_progress_scope(tracker):
        try:
            yield
            tracker.step("ready", "running")
            tracker.step("ready", "passed")
            tracker.finish()
        except BaseException as exc:
            tracker.finish("failed", connection_step_error(exc))
            raise


def validate_channel_connection(channel: str, config: BaseAccountConfig, report: ConnectionProgress) -> None:
    """构造前注入观察者，完成真实核对后释放临时执行器。"""
    executor = None
    with validation_progress(channel, report):
        try:
            executor = get_channel(channel).create_executor(config)
            if not executor._verify_connection():
                executor._initialize_connection(config)
            reconcile_account_connection(executor)
        finally:
            if executor is not None:
                connection_monitor(executor).close()
                close = getattr(executor, "close", None)
                if callable(close):
                    close()
