"""在公共报撤接口外侧观察调用，保留账户控制拒绝及同步身份。"""

from collections.abc import Callable
from enum import Enum
from functools import wraps
from inspect import signature
from math import isfinite

from axile.executor.order_submission_context import get_order_submission_context
from axile.executor.replay import observe, replay_command, replay_enabled


def _intent_value(value: object) -> object:
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int) or isinstance(value, float) and isfinite(value):
        return value
    return None


def _action_context(owner: object) -> dict[str, object]:
    try:
        context = get_order_submission_context()
        if context and isinstance(context.get("execution_id"), str):
            return {"execution_id": context["execution_id"], "origin": "execution"}
        getter = getattr(owner, "get_account_control_guard", None)
        guard = getter() if callable(getter) else getattr(owner, "_account_control_guard", None)
        if getattr(guard, "source", None) == "manual_cancel":
            return {"origin": "manual"}
        execution_id = getattr(guard, "execution_id", None)
        if getattr(guard, "context_kind", None) == "execution" and isinstance(execution_id, str):
            return {"execution_id": execution_id, "origin": "execution"}
    except Exception:  # noqa: BLE001 - 读取旁路上下文不能改变报撤结果
        pass
    return {"origin": "unknown"}


def replay_operation[**P, R](operation: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """覆盖整个报撤调用；关闭时直接委托，录制错误不改变交易结果。"""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        call_signature = signature(function)

        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            owner = args[0] if args else None
            if owner is None or not replay_enabled(owner):
                return function(*args, **kwargs)
            arguments = call_signature.bind_partial(*args, **kwargs).arguments
            fields = {
                key: _intent_value(arguments[key])
                for key in ("symbol", "direction", "order_type", "price", "order_id")
                if key in arguments
            }
            fields.update(_action_context(owner))
            if "volume" in arguments:
                fields["quantity"] = _intent_value(arguments["volume"])
            with replay_command(owner, operation, fields) as command_id:
                result = function(*args, **kwargs)
            # 返回值只关联同步结果，不能据此确认柜台受理或撤单生效。
            observe(
                owner,
                "/command",
                {
                    **fields,
                    "command_id": command_id,
                    "operation": operation,
                    "phase": "result",
                    "order_id": getattr(result, "order_id", fields.get("order_id")),
                    "success": result if isinstance(result, bool) else None,
                },
            )
            return result

        return wrapper

    return decorate
