"""按账户顺序派发持久化通知，不重试失败或未知结果。"""

import asyncio

import loguru

from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE
from axile.common.notification_context import ACCOUNT_CONTROL_WARNING_EVENT
from axile.common.notification_function import NotificationFunctionResult, run_notification_function
from axile.executor.algorithms.utils.clock import clock_now
from axile.server.native_store import native_operation

_tasks: dict[int, asyncio.Task[None]] = {}
recovering = False
_rerun: set[int] = set()


def wake_notifications(account_id: int) -> None:
    """为账户启动一个按持久化顺序消费的通知运行器。"""
    if recovering:
        return
    task = _tasks.get(account_id)
    if task is None or task.done():
        _tasks[account_id] = asyncio.create_task(_dispatch(account_id))
    else:
        _rerun.add(account_id)


async def _dispatch(account_id: int) -> None:
    """原生提交 calling 后才调用用户函数，发送摘要失败不改变结果。"""
    try:
        while event := await native_operation("notifications_claim", {"account_id": account_id}):
            context = event["context"]
            code, key = event["code"], event["feishu_key"]
            result = None
            if event["event_type"] == ACCOUNT_CONTROL_WARNING_EVENT:
                from axile.server.error_notifications import send_system_notification

                result = await send_system_notification(context)
            elif code:
                result = (
                    NotificationFunctionResult(False, "未配置飞书 Webhook，无法发送通知")
                    if code == DEFAULT_ACCOUNT_NOTIFICATION_CODE and not key
                    else await asyncio.to_thread(run_notification_function, code, context, feishu_key=key)
                )
            receipt = await native_operation(
                "notifications_finish",
                {
                    "sequence": event["sequence"],
                    "finished_at": clock_now().isoformat(),
                    "ok": result is None or result.ok,
                    "error": None if result is None else result.error,
                    "record_summary": result is not None and event["event_type"] != ACCOUNT_CONTROL_WARNING_EVENT,
                },
            )
            if receipt.get("summary_error"):
                loguru.logger.warning("通知结果已保存，更新摘要失败: {}", receipt["summary_error"])
    except Exception:
        loguru.logger.exception("通知 outbox 派发中断 account_id={}", account_id)
    finally:
        if _tasks.get(account_id) is asyncio.current_task():
            _tasks.pop(account_id, None)
        if account_id in _rerun:
            _rerun.discard(account_id)
            asyncio.get_running_loop().call_soon(wake_notifications, account_id)


async def recover_notifications() -> None:
    """恢复未开始事件；调用结果不明的事件永久标记 unknown。"""
    result = await native_operation("notifications_recover", {"finished_at": clock_now().isoformat()})
    for error in result["summary_errors"]:
        loguru.logger.warning("通知事件已保留为未知，恢复摘要失败: {}", error)
    for account_id in result["account_ids"]:
        wake_notifications(account_id)
