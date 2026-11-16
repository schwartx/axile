"""
执行结果模型.

定义统一执行状态、单个品种的算法结果，以及成功态判断辅助函数。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, cast

from msgspec import UNSET, Meta

from axile.domain.execution import ExecutionReasonFamily
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.models.value_conversion import PriceValueConversion


class ExecutionStatus(StrEnum):
    """
    统一执行状态枚举.

    Attributes
    ----------
    SUCCEEDED : str
        执行成功并完成预期动作。
    NOOP : str
        无需执行任何动作。
    BLOCKED : str
        因前置条件不满足而阻塞。
    PARTIAL : str
        已有执行进展，但未完成全部执行要求；可能持仓已到位而订单收尾失败。
    FAILED : str
        执行失败。
    """

    SUCCEEDED = "SUCCEEDED"
    NOOP = "NOOP"
    BLOCKED = "BLOCKED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class ExecutionOutcome(StrEnum):
    """面向用户的执行结论；新结果一律由 :func:`outcome_from_status` 派生，不独立计算。"""

    COMPLETED = "completed"
    NOT_REACHED = "not_reached"
    ERROR = "error"
    TERMINATED = "terminated"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


_OUTCOME_FROM_STATUS: dict[ExecutionStatus, ExecutionOutcome] = {
    ExecutionStatus.SUCCEEDED: ExecutionOutcome.COMPLETED,
    ExecutionStatus.NOOP: ExecutionOutcome.COMPLETED,
    ExecutionStatus.BLOCKED: ExecutionOutcome.BLOCKED,
    ExecutionStatus.PARTIAL: ExecutionOutcome.NOT_REACHED,
    ExecutionStatus.FAILED: ExecutionOutcome.ERROR,
}


def outcome_from_status(status: ExecutionStatus, *, terminated: bool = False) -> ExecutionOutcome:
    """
    从控制流状态派生展示结论.

    Parameters
    ----------
    status : ExecutionStatus
        控制流状态，作为展示结论的唯一真源。
    terminated : bool, optional
        执行是否因用户终止而停止；终止优先于状态映射。

    Returns
    -------
    ExecutionOutcome
        与状态唯一对应的展示结论。
    """
    if terminated:
        return ExecutionOutcome.TERMINATED
    return _OUTCOME_FROM_STATUS[status]


class TargetSizingStatus(StrEnum):
    """目标数量换算状态."""

    SIZED = "SIZED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(kw_only=True)
class TargetSizingDecision:
    """单品种从账户目标到可执行数量的结构化换算证据."""

    symbol: Annotated[str, Meta(description="品种代码")] = field(default="")
    sizing_mode: Annotated[str, Meta(description="目标输入口径")] = field(default="weight")
    status: Annotated[TargetSizingStatus, Meta(description="换算状态")] = field(default=TargetSizingStatus.SIZED)
    reason_code: Annotated[str, Meta(description="结构化换算原因")] = field(default="COMMON.SIZING.EXACT")
    account_weight: Annotated[float, Meta(description="账户口径目标权重或直接数量")] = field(default=0.0)
    equity: Annotated[float, Meta(description="换算时账户权益")] = field(default=0.0)
    reference_price: Annotated[float | None, Meta(description="换算使用的参考价格")] = field(default=None)
    unit_multiplier: Annotated[float | None, Meta(description="价格到单位名义价值的乘数")] = field(default=None)
    unit_notional: Annotated[float | None, Meta(description="每个交易单位的名义价值")] = field(default=None)
    target_notional: Annotated[float | None, Meta(description="账户权重对应的目标名义价值")] = field(default=None)
    raw_quantity: Annotated[float | None, Meta(description="应用数量规则前的目标数量")] = field(default=None)
    target_quantity: Annotated[float | None, Meta(description="应用数量规则后的目标数量")] = field(default=None)
    current_quantity: Annotated[float | None, Meta(description="规划时当前带号持仓数量")] = field(default=None)
    quantity_step: Annotated[float | None, Meta(description="数量步长或整手单位")] = field(default=None)
    min_quantity: Annotated[float | None, Meta(description="最小可交易数量")] = field(default=None)
    min_notional: Annotated[float | None, Meta(description="最小可交易名义价值")] = field(default=None)
    value_conversion: Annotated[PriceValueConversion | None, Meta(description="规划时冻结的金额换算依据")] = field(
        default=None
    )


def is_success_status(status: ExecutionStatus) -> bool:
    """
    判断执行状态是否属于成功态.

    Parameters
    ----------
    status : ExecutionStatus
        待判断的执行状态。

    Returns
    -------
    bool
        当状态属于成功集合时返回 ``True``。
    """
    return status in {ExecutionStatus.SUCCEEDED, ExecutionStatus.NOOP}


@dataclass(kw_only=True)
class AlgorithmResult:
    """
    单个品种的算法执行结果.

    Attributes
    ----------
    symbol : str
        结果对应的品种代码。
    algorithm : str
        实际执行的算法名称。
    orders : list[UnifiedOrder]
        该品种产生的订单列表。
    trades : list[TradeRecord]
        该品种产生的成交明细列表。
    status : ExecutionStatus
        该品种的执行状态。
    error : str | None
        执行失败时的错误信息。
    """

    outcome: Annotated[ExecutionOutcome, Meta(description="执行展示结论")] = field(
        default=cast(ExecutionOutcome, UNSET)
    )
    outcome_reason: Annotated[str | None, Meta(description="过程错误或未到位的具体原因")] = field(
        default=cast("str | None", UNSET)
    )
    final_volume: Annotated[float | None, Meta(description="结束时实际持仓；缺少证据时为空")] = field(default=None)
    start_volume: Annotated[float | None, Meta(description="算法开始下单前实际读取的带号持仓")] = field(default=None)
    expected_volume: Annotated[float | None, Meta(description="起始持仓与已确认终态订单净成交推导的仓位")] = field(
        default=None
    )
    position_confirmed: Annotated[bool | None, Meta(description="渠道持仓是否与成交证据一致；未核对时为空")] = field(
        default=None
    )
    symbol: Annotated[str, Meta(description="品种代码")] = field(default="")
    algorithm: Annotated[str, Meta(description="实际执行的算法名")] = field(default="")
    orders: Annotated[list[UnifiedOrder], Meta(description="该品种产生的订单")] = field(default_factory=list)
    trades: Annotated[list[TradeRecord], Meta(description="该品种产生的成交明细")] = field(default_factory=list)
    target_volume: Annotated[int | float | None, Meta(description="该品种目标持仓数量")] = field(default=None)
    sizing: Annotated[TargetSizingDecision | None, Meta(description="目标数量换算证据")] = field(default=None)
    first_tick: Annotated[UnifiedPriceData | None, Meta(description="该品种首笔行情快照")] = field(default=None)
    memory: Annotated[dict[str, object], Meta(description="算法附加信息")] = field(default_factory=dict)
    status: Annotated[ExecutionStatus, Meta(description="该品种执行状态")] = field(default=ExecutionStatus.SUCCEEDED)
    error: Annotated[str | None, Meta(description="该品种失败原因")] = field(default=None)
    reason_code: Annotated[str | None, Meta(description="该品种失败或受阻的稳定原因码")] = field(default=None)
    reason_family: Annotated[ExecutionReasonFamily | None, Meta(description="该品种失败或受阻的原因分类")] = field(
        default=None
    )
    account_assets: Annotated[
        UnifiedAccountAssets | None, Meta(description="执行结束时的账户资产快照，仅供运行时使用")
    ] = field(default=None, metadata={"exclude": True}, repr=False)

    def __post_init__(self) -> None:
        """未显式给出 outcome/outcome_reason 时从 status 与 error 派生，保证两套结论不分离.

        Warning
        -------
        派生只发生在构造与校验时；构造之后修改 ``status``（直接赋值或
        ``copy_data(..., update=...)``）不会触发重派生，会得到分离结论。
        需要改状态时必须重建实例；落库边界 ``records``
        会按当前 status 重派生兜底。
        """
        if self.outcome is UNSET:
            self.outcome = outcome_from_status(self.status)
        if self.outcome_reason is UNSET:
            self.outcome_reason = self.error

    @property
    def success(self) -> bool:
        """
        返回当前结果是否属于成功态.

        Returns
        -------
        bool
            当 ``status`` 属于成功集合时返回 ``True``。
        """
        return is_success_status(self.status)

    __json_properties__ = ("success",)
