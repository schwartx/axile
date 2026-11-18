"""在调用账户通知脚本前加载显式声明的数据。"""

from math import isfinite
from typing import Any

from axile.common.notification_function import (
    NotificationFunctionResult,
    run_notification_function,
    validate_notification_function,
)
from axile.server._native import _core
from axile.server.native_store import native_operation_sync


def requires_notification_data(code: str | None) -> bool:
    """保持未声明附加数据的执行器通知路径。"""
    if not code or "NOTIFICATION_REQUIRES" not in code:
        return False
    try:
        return bool(validate_notification_function(code))
    except ValueError:
        return False


def run_account_notification(
    code: str, context: dict[str, Any], *, account_id: int, feishu_key: str | None
) -> NotificationFunctionResult:
    """正式通知与试跑共享的入口。"""

    def load_context(requirements: tuple[str, ...]) -> dict[str, object]:
        if context.get("event_type") != "execution.finished":
            return {}
        execution = context.get("execution") or {}
        additions: dict[str, object] = {}
        assets = None
        if "daily_return" in requirements:
            additions["performance"] = {"daily_return": None}
            observation = context.get("assets") or {}
            total, when = observation.get("total_assets"), observation.get("update_time")
            if type(total) in (float, int) and isfinite(total) and isinstance(when, str):
                assets = {"total_assets": total, "update_time": when}
        history = []
        if "supplement_history" in requirements:
            additions["supplement_history"] = []
            history = [item for item in execution.get("supplements") or [] if item.get("is_last") is True]
        if not execution.get("id") or (not assets and not history):
            return additions
        try:
            data = native_operation_sync(
                "notifications_context_data",
                {"account_id": account_id, "execution_id": execution["id"], "assets": assets, "groups": history},
            )
        except _core.NativeError as exc:
            raise ValueError(f"加载通知附加数据失败：{exc.args[1]}") from exc
        return additions | data

    return run_notification_function(code, context, feishu_key=feishu_key, load_context=load_context)
