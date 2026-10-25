"""
执行结果模型.

定义统一执行状态、单个品种的算法结果，以及成功态判断辅助函数。
"""

from enum import StrEnum

from pydantic import BaseModel, Field, computed_field, model_validator

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


class TargetSizingDecision(BaseModel):
    """单品种从账户目标到可执行数量的结构化换算证据."""

    symbol: str = Field(default="", description="品种代码")
    sizing_mode: str = Field(default="weight", description="目标输入口径")
    status: TargetSizingStatus = Field(default=TargetSizingStatus.SIZED, description="换算状态")
    reason_code: str = Field(default="COMMON.SIZING.EXACT", description="结构化换算原因")
    account_weight: float = Field(default=0.0, description="账户口径目标权重或直接数量")
    equity: float = Field(default=0.0, description="换算时账户权益")
    reference_price: float | None = Field(default=None, description="换算使用的参考价格")
    unit_multiplier: float | None = Field(default=None, description="价格到单位名义价值的乘数")
    unit_notional: float | None = Field(default=None, description="每个交易单位的名义价值")
    target_notional: float | None = Field(default=None, description="账户权重对应的目标名义价值")
    raw_quantity: float | None = Field(default=None, description="应用数量规则前的目标数量")
    target_quantity: float | None = Field(default=None, description="应用数量规则后的目标数量")
    current_quantity: float | None = Field(default=None, description="规划时当前带号持仓数量")
    quantity_step: float | None = Field(default=None, description="数量步长或整手单位")
    min_quantity: float | None = Field(default=None, description="最小可交易数量")
    min_notional: float | None = Field(default=None, description="最小可交易名义价值")
    value_conversion: PriceValueConversion | None = Field(default=None, description="规划时冻结的金额换算依据")


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


class AlgorithmResult(BaseModel):
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

    outcome: ExecutionOutcome = Field(default=ExecutionOutcome.UNKNOWN, description="执行展示结论")
    outcome_reason: str | None = Field(default=None, description="过程错误或未到位的具体原因")
    final_volume: float | None = Field(default=None, description="结束时实际持仓；缺少证据时为空")
    symbol: str = Field(default="", description="品种代码")
    algorithm: str = Field(default="", description="实际执行的算法名")
    orders: list[UnifiedOrder] = Field(default_factory=list, description="该品种产生的订单")
    trades: list[TradeRecord] = Field(default_factory=list, description="该品种产生的成交明细")
    target_volume: int | float | None = Field(default=None, description="该品种目标持仓数量")
    sizing: TargetSizingDecision | None = Field(default=None, description="目标数量换算证据")
    first_tick: UnifiedPriceData | None = Field(default=None, description="该品种首笔行情快照")
    memory: dict[str, object] = Field(default_factory=dict, description="算法附加信息")
    status: ExecutionStatus = Field(default=ExecutionStatus.SUCCEEDED, description="该品种执行状态")
    error: str | None = Field(default=None, description="该品种失败原因")
    reason_code: str | None = Field(default=None, description="该品种失败或受阻的稳定原因码")
    account_assets: UnifiedAccountAssets | None = Field(
        default=None,
        exclude=True,
        repr=False,
        description="执行结束时的账户资产快照，仅供运行时使用",
    )

    @model_validator(mode="after")
    def _derive_outcome_from_status(self) -> "AlgorithmResult":
        """未显式给出 outcome/outcome_reason 时从 status 与 error 派生，保证两套结论不分离.

        Warning
        -------
        派生只发生在构造与校验时；构造之后修改 ``status``（直接赋值或
        ``model_copy(update=...)``）不会触发重派生，会得到分离结论。
        需要改状态时必须重建实例；落库边界 ``records``
        会按当前 status 重派生兜底。
        """
        if "outcome" not in self.model_fields_set:
            self.outcome = outcome_from_status(self.status)
        if "outcome_reason" not in self.model_fields_set and self.error is not None:
            self.outcome_reason = self.error
        return self

    @computed_field  # type: ignore[prop-decorator]
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
