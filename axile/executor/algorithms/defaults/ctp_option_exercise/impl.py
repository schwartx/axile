"""``CTP_OPTION_EXERCISE`` 算法实现.

针对单一期权 symbol 提交行权 / 放弃 / 自对冲指令，并轮询
``CTPExecutor`` 的轻量期权记录直至进入终态。

Notes
-----
本算法只关心单品种期权指令的落地执行；目标仓位 / 期权识别由
``CTPExecutor._calculate_generic_volume`` 与 ``classify`` 工具在算法上层
解决。算法签名仍按统一注册表的形态。

调用方约定（``UnifiedStandardInput``）::

    symbol_algorithms={
        "m2510-C-3000": {
            "method": "CTP_OPTION_EXERCISE",
            "params": {"action": "exercise"},
        },
    }
    curr_target={"m2510-C-3000": 5}      # 目标行权张数
    trade_rules={"m2510-C-3000": {"sizing_mode": "lots"}}

target_volume 表示要行权 / 放弃 / 自对冲的张数（必须为非负整数）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Protocol, cast

from msgspec import Meta

from axile.common.trade_channel import TradeChannel
from axile.executor.algorithms.common.params import BaseAlgorithmParams
from axile.executor.algorithms.core.base import (
    AlgorithmInput,
    ExecutorProtocol,
    register_algorithm,
)
from axile.executor.algorithms.exceptions import execution_error_message
from axile.executor.algorithms.utils.clock import get_default_clock
from axile.executor.models.execution_result import AlgorithmResult, ExecutionStatus
from axile.executor.termination import ExecutionTerminated

if TYPE_CHECKING:
    # 算法注册和元数据查询不能触发可选渠道 SDK 导入；此类型仅供静态检查使用。
    from axile.executor.ctp.options import OptionActionRecord


class CtpOptionExecutorProtocol(ExecutorProtocol, Protocol):
    """期权算法依赖的 CTP 扩展契约。"""

    def submit_option_action(self, symbol: str, action: str, volume: int, **kwargs: object) -> OptionActionRecord:
        """提交期权指令。"""
        ...

    def cancel_option_action(self, order_ref: str) -> bool:
        """撤销期权指令。"""
        ...

    def get_option_action_status(self, order_ref: str) -> OptionActionRecord | None:
        """查询期权指令状态。"""
        ...

    def is_exercise_valuable(self, symbol: str) -> bool:
        """判断期权是否有行权价值。"""
        ...


@dataclass(kw_only=True)
class CTPOptionExerciseParams(BaseAlgorithmParams):
    """``CTP_OPTION_EXERCISE`` 参数."""

    max_wait_seconds: Annotated[
        int,
        Meta(
            ge=1,
            le=3600,
            title="最大等待",
            description="等待指令终态的上限；等待结束不等于指令完成。",
            extra_json_schema={"x-order": 40, "x-unit": "秒", "x-control": "stepper"},
        ),
    ] = field(default=60)

    action: Annotated[
        str,
        Meta(
            title="指令类型",
            description="行权、放弃或自对冲；按指定张数执行。",
            extra_json_schema={
                "x-order": 10,
                "x-control": "choice",
                "x-enum-labels": {"exercise": "行权", "abandon": "放弃", "self_close": "自对冲"},
            },
        ),
    ] = field(default="exercise")
    require_value_check: Annotated[
        bool,
        Meta(
            title="检查内在价值",
            description="仅行权时生效；关闭后跳过内在价值检查，不代表完整收益评估。",
            extra_json_schema={"x-order": 20},
        ),
    ] = field(default=True)
    poll_interval_seconds: Annotated[
        float,
        Meta(
            title="查询间隔",
            description="查询指令状态的间隔。",
            extra_json_schema={"x-order": 30, "x-unit": "秒", "x-control": "stepper"},
        ),
    ] = field(default=0.5)

    def __str__(self) -> str:
        """日志友好的字符串。"""
        return (
            f"CTPOptionExerciseParams(action={self.action}, "
            f"max_wait_seconds={self.max_wait_seconds}, "
            f"require_value_check={self.require_value_check}, "
            f"poll_interval_seconds={self.poll_interval_seconds})"
        )


def _resolve_params(raw: object | None) -> CTPOptionExerciseParams:
    """把 ``algorithm.params`` 中的字典 / 已解析对象统一成参数实例。"""
    if isinstance(raw, CTPOptionExerciseParams):
        return raw
    if isinstance(raw, dict):
        return CTPOptionExerciseParams(**cast(dict[str, Any], raw))
    if raw is None:
        return CTPOptionExerciseParams()
    raise TypeError(f"CTP_OPTION_EXERCISE 不支持的 params 类型: {type(raw).__name__}")


def _wait_for_terminal(
    executor: CtpOptionExecutorProtocol,
    order_ref: str,
    max_wait_seconds: float,
    poll_interval_seconds: float,
):
    """轮询行权指令状态机，超时或终态返回最新 record。"""
    clock = get_default_clock()
    deadline = clock.time() + max_wait_seconds
    record = executor.get_option_action_status(order_ref)
    while record is not None and not _is_terminal(record):
        if executor.is_termination_requested():
            executor.handle_termination_checkpoint()
            break
        if clock.time() >= deadline:
            executor.logger.warning(f"行权指令 ref={order_ref} 在 {max_wait_seconds}s 内未进入终态，结束等待")
            break
        executor.sleep_or_terminate(poll_interval_seconds)
        record = executor.get_option_action_status(order_ref)
    return record


def _is_terminal(record: object) -> bool:
    """无类型耦合的终态判断。"""
    status = getattr(record, "status", None)
    if status is None:
        return False
    return getattr(status, "value", str(status)) in {
        "executed",
        "abandoned",
        "cancelled",
        "failed",
    }


@register_algorithm(
    "CTP_OPTION_EXERCISE",
    channels=[TradeChannel("ctp")],
    params_class=CTPOptionExerciseParams,
    slots=[],
    label="CTP 期权行权",
    description="提交期权行权、放弃或自对冲指令，并等待状态回报。按指定张数执行，不用于账户主交易或清仓。",
)
def ctp_option_exercise_algorithm(executor: ExecutorProtocol, algorithm_input: AlgorithmInput) -> AlgorithmResult:
    """
    针对单一期权 symbol 提交行权 / 放弃 / 自对冲指令.

    Parameters
    ----------
    executor : ExecutorProtocol
        当前执行器会话。运行时必须是 ``CTPExecutor``，因为算法依赖
        ``submit_option_action`` / ``get_option_action_status`` /
        ``is_exercise_valuable`` 这些 CTP 专属方法。
    algorithm_input : AlgorithmInput
        统一输入。``target_volume`` 表示张数；``params`` 解析为
        :class:`CTPOptionExerciseParams`。

    Returns
    -------
    AlgorithmResult
        算法执行结果。``orders`` 留空（行权不入普通订单流），状态机的
        最终态写入 ``memory['option_action']``。
    """
    symbol = algorithm_input.symbol
    target = int(algorithm_input.target_volume) if algorithm_input.target_volume else 0

    params = _resolve_params(algorithm_input.params)

    if target <= 0:
        executor.logger.info(f"期权指令 {symbol} 目标张数={target}，跳过")
        return AlgorithmResult(
            symbol=symbol,
            algorithm="CTP_OPTION_EXERCISE",
            status=ExecutionStatus.NOOP,
        )

    # 行权前的内在价值检查：避免无价值行权造成账户损失
    if params.action == "exercise" and params.require_value_check:
        option_executor = cast(CtpOptionExecutorProtocol, executor)
        valuable = option_executor.is_exercise_valuable(symbol)
        if not valuable:
            executor.logger.warning(f"期权 {symbol} 当前无内在价值，跳过行权（如确需行权请关闭 require_value_check）")
            return AlgorithmResult(
                symbol=symbol,
                algorithm="CTP_OPTION_EXERCISE",
                status=ExecutionStatus.BLOCKED,
                error="期权无内在价值，已跳过行权",
            )

    try:
        option_executor = cast(CtpOptionExecutorProtocol, executor)
        record = option_executor.submit_option_action(
            symbol=symbol,
            action=params.action,
            volume=target,
            trade_rule=algorithm_input.trade_rule,
        )
    except (RuntimeError, ValueError) as exc:
        if isinstance(exc, ExecutionTerminated) or getattr(exc, "requires_session_recovery", False):
            raise
        executor.logger.error(f"期权指令提交失败 {symbol} action={params.action}: {exc}")
        return AlgorithmResult(
            symbol=symbol,
            algorithm="CTP_OPTION_EXERCISE",
            status=ExecutionStatus.FAILED,
            error=execution_error_message(exc, "期权指令提交"),
            memory={"error": str(exc)},
        )

    final = _wait_for_terminal(
        cast(CtpOptionExecutorProtocol, executor),
        record.order_ref,
        max_wait_seconds=float(params.max_wait_seconds),
        poll_interval_seconds=float(params.poll_interval_seconds),
    )

    final_status_value: str
    if final is None:
        final_status_value = "unknown"
        algo_status = ExecutionStatus.FAILED
        error: str | None = "期权指令最终状态尚未确认"
    else:
        final_status_value = getattr(final.status, "value", str(final.status))
        if final_status_value == "executed":
            algo_status = ExecutionStatus.SUCCEEDED
            error = None
        elif final_status_value == "abandoned":
            algo_status = ExecutionStatus.SUCCEEDED
            error = None
        elif final_status_value in ("cancelled", "failed"):
            algo_status = ExecutionStatus.FAILED
            error = "期权指令已撤销" if final_status_value == "cancelled" else "期权指令执行失败"
            if final.error_id:
                error += f"，错误码 {final.error_id}"
        else:
            # 未到终态（轮询超时）
            algo_status = ExecutionStatus.FAILED
            error = "期权指令等待超时，最终状态尚未确认"

    memory_payload: dict[str, object] = {
        "option_action": {
            "order_ref": record.order_ref,
            "action": params.action,
            "volume": target,
            "final_status": final_status_value,
            "error_id": getattr(final, "error_id", 0) if final is not None else 0,
            "error_msg": getattr(final, "error_msg", "") if final is not None else "",
            "error_source": getattr(final, "error_source", "") if final is not None else "",
        }
    }

    executor.logger.info(f"期权指令 {symbol} ref={record.order_ref} action={params.action} 终态={final_status_value}")

    return AlgorithmResult(
        symbol=symbol,
        algorithm="CTP_OPTION_EXERCISE",
        status=algo_status,
        target_volume=target,
        memory=memory_payload,
        error=error,
    )
