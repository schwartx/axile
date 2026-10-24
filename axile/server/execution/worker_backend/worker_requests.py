"""解析 worker 协议请求并构造强类型上下文.

该模块负责把跨进程传输的松散字典载荷收敛为 worker 内部使用的
不可变快照，避免后续执行路径反复依赖弱类型字段访问。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from axile.executor.models.unified_input import UnifiedStandardInput
from axile.server.db.models.account import AccountSnapshot
from axile.server.execution.worker_backend.protocol import WorkerBackendRequest


@dataclass(frozen=True, slots=True)
class _ExecuteTradeRequestContext:
    """保存 `execute_trade` 请求解析后的上下文快照.

    Notes
    -----
    该快照只保存当前命令真正需要的字段，避免把原始协议对象继续
    传递到后续执行链路中。
    """

    account: AccountSnapshot
    algorithm_name: str
    trigger_source: str
    standard_input: UnifiedStandardInput
    audit_context: dict[str, object]
    audit_input: dict[str, object]
    notification_snapshot: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _EmptyPositionsRequestContext:
    """保存 `empty_positions` 请求解析后的上下文快照.

    Notes
    -----
    清仓路径不会先构造完整的 `UnifiedStandardInput`，因此保留原始
    `empty_kwargs` 供执行器直接消费。
    """

    account: AccountSnapshot
    empty_kwargs: dict[str, object]
    algorithm_name: str
    audit_context: dict[str, object]
    audit_input: dict[str, object]
    notification_snapshot: dict[str, object] | None = None


def _parse_execute_trade_request(request: WorkerBackendRequest) -> _ExecuteTradeRequestContext:
    """
    解析 worker `execute_trade` 请求载荷。

    Parameters
    ----------
    request : WorkerBackendRequest
        主进程发来的执行交易请求。

    Returns
    -------
    _ExecuteTradeRequestContext
        已转为强类型字段的请求上下文。
    """
    account = AccountSnapshot.model_validate(request.account_payload)
    # 同一份 payload 在不同交易渠道下会走不同字段归一化逻辑，这里必须显式带上账户渠道。
    standard_input = UnifiedStandardInput.from_dict(
        cast(dict[str, object], request.payload["standard_input"]),
        channel_type=account.trade_channel,
    )
    # 审计上下文是请求级元数据，不属于统一输入模型的交易语义字段，
    # 但后续审计补写和执行器 runtime 绑定都依赖它。
    audit_context = cast(dict[str, object], cast(dict[str, object], standard_input.extra).get("audit", {}))
    return _ExecuteTradeRequestContext(
        account=account,
        algorithm_name=str(account.algorithm.get("method", "SINGLE-MAKER")),
        trigger_source=cast(str, request.payload["trigger_source"]),
        standard_input=standard_input,
        audit_context=audit_context,
        audit_input=cast(dict[str, object], request.payload["audit_input"]),
        notification_snapshot=cast("dict[str, object] | None", request.payload.get("notification_snapshot")),
    )


def _parse_empty_positions_request(request: WorkerBackendRequest) -> _EmptyPositionsRequestContext:
    """
    解析 worker `empty_positions` 请求载荷。

    Parameters
    ----------
    request : WorkerBackendRequest
        主进程发来的清仓请求。

    Returns
    -------
    _EmptyPositionsRequestContext
        已转为强类型字段的请求上下文。
    """
    account = AccountSnapshot.model_validate(request.account_payload)
    empty_kwargs = cast(dict[str, object], request.payload["empty_kwargs"])
    resolved_algorithm = cast(dict[str, object], empty_kwargs["algorithm"])
    # 清仓输入不是完整 UnifiedStandardInput，而是入口临时拼出来的 kwargs，
    # 审计上下文也必须从同一份原始数据里回读，避免与真实执行参数脱节。
    audit_context = cast(
        dict[str, object],
        cast(dict[str, object], cast(dict[str, object], empty_kwargs["extra"])["audit"]),
    )
    return _EmptyPositionsRequestContext(
        account=account,
        empty_kwargs=empty_kwargs,
        algorithm_name=str(resolved_algorithm.get("method", "SINGLE-MAKER")),
        audit_context=audit_context,
        audit_input=cast(dict[str, object], request.payload["audit_input"]),
        notification_snapshot=cast("dict[str, object] | None", request.payload.get("notification_snapshot")),
    )
