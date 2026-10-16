"""
统一标准输出数据模型.

封装执行器的账户级输出结果，并通过 ``symbol_results`` 聚合各品种执行细节。
"""

from datetime import datetime
from typing import Any, TypedDict, override

from pydantic import BaseModel, ConfigDict, Field, model_validator

from axile.common.trade_channel import TradeChannel
from axile.executor.models.execution_result import (
    AlgorithmResult,
    ExecutionOutcome,
    ExecutionStatus,
    is_success_status,
    outcome_from_status,
)
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.session_closed import COMMON_SESSION_CLOSED


class ExtraData(TypedDict, total=False):
    """附加数据字典类型定义."""

    error: str
    message: str
    channel_type: str


def _derive_output_error(
    symbol_results: dict[str, AlgorithmResult],
    status: ExecutionStatus,
) -> str | None:
    """
    根据整体状态与子结果推导顶层错误信息.

    Parameters
    ----------
    symbol_results : dict[str, AlgorithmResult]
        各品种的执行结果映射。
    status : ExecutionStatus
        当前整体执行状态。

    Returns
    -------
    str | None
        推导出的错误信息；若不存在则返回 ``None``。
    """
    if is_success_status(status):
        return None
    for result in symbol_results.values():
        if not is_success_status(result.status) and result.error:
            return result.error

    return None


def _derive_output_reason_code(
    symbol_results: dict[str, AlgorithmResult],
    status: ExecutionStatus,
) -> str | None:
    """仅整单 BLOCKED 且未成功品种原因码完全相同时提升到账户级。"""
    if status != ExecutionStatus.BLOCKED:
        return None
    failed = [result for result in symbol_results.values() if not is_success_status(result.status)]
    if not failed:
        return None
    codes = {result.reason_code for result in failed}
    if len(codes) != 1:
        return None
    code = next(iter(codes))
    if code == COMMON_SESSION_CLOSED and any(result.orders or result.trades for result in failed):
        return None
    return code if isinstance(code, str) and code else None


class UnifiedStandardOutput(BaseModel):
    """
    统一标准输出数据模型.

    Attributes
    ----------
    account_assets : UnifiedAccountAssets
        执行完成后的账户资产快照。
    symbol_results : dict[str, AlgorithmResult]
        各品种维度的执行结果映射。
    status : ExecutionStatus
        整体执行状态。
    error : str | None
        整体失败原因。
    execution_time : float
        本次执行耗时，单位为秒。
    channel_type : TradeChannel
        本次执行所属的交易渠道。
    """

    model_config = ConfigDict(
        json_encoders={datetime: lambda v: v.isoformat() if v else ""},
    )

    # === 核心字段（与现有standard_output完全一致） ===
    account_assets: UnifiedAccountAssets = Field(..., description="账户资产信息")
    memory: dict[str, Any] = Field(default_factory=dict, description="执行过程中的附加信息")
    inputs: UnifiedStandardInput | None = Field(None, description="原始输入参数")
    symbol_results: dict[str, AlgorithmResult] = Field(default_factory=dict, description="各品种执行结果")
    status: ExecutionStatus = Field(..., description="本次执行的整体状态")
    error: str | None = Field(default=None, description="本次执行的整体失败原因")
    reason_code: str | None = Field(default=None, description="整体失败或受阻的稳定原因码；原因不同时为空")
    outcome: ExecutionOutcome | None = Field(default=None, description="执行展示结论")
    outcome_reason: str | None = Field(default=None, description="展示结论的具体原因")

    # === 元数据字段 ===
    execution_time: float = Field(default=0.0, ge=0.0, description="执行耗时（秒）")
    channel_type: TradeChannel = Field(..., description="交易渠道类型")
    success: bool = Field(default=True, description="执行是否成功")

    # === 渠道特有数据 ===
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="渠道特有数据，包含原始输出和其他特定信息",
    )

    @override
    def __str__(self) -> str:
        """返回便于记录日志的简要字符串表示."""
        # 处理枚举类型
        channel = self.channel_type.value if hasattr(self.channel_type, "value") else str(self.channel_type)
        return (
            f"UnifiedStandardOutput("
            f"channel={channel}, "
            f"success={self.success}, "
            f"orders={len(self.orders)}, "
            f"symbols={len(self.symbol_results)})"
        )

    # ==================== 验证器 ====================

    @model_validator(mode="after")
    def validate_consistency(self) -> "UnifiedStandardOutput":
        """
        校验并补齐输出对象的派生字段.

        Returns
        -------
        UnifiedStandardOutput
            校验完成后的当前输出对象。
        """
        # 如果channel_type不在extra中，添加进去
        if "channel_type" not in self.extra:
            channel_value = self.channel_type.value if hasattr(self.channel_type, "value") else str(self.channel_type)
            self.extra["channel_type"] = channel_value

        if ("error" not in self.model_fields_set or self.error is None) and self.error is None:
            self.error = _derive_output_error(self.symbol_results, self.status)

        if "reason_code" not in self.model_fields_set or self.reason_code is None:
            self.reason_code = _derive_output_reason_code(self.symbol_results, self.status)

        if self.outcome is None:
            self.outcome = outcome_from_status(self.status)
        if self.outcome_reason is None:
            self.outcome_reason = next(
                (
                    result.outcome_reason
                    for result in self.symbol_results.values()
                    if result.outcome == self.outcome and result.outcome_reason
                ),
                None,
            )
        self.success = is_success_status(self.status)

        return self

    @property
    def orders(self) -> list[UnifiedOrder]:
        """
        聚合所有品种结果中的订单.

        Returns
        -------
        list[UnifiedOrder]
            当前输出中全部订单的扁平列表。
        """
        orders: list[UnifiedOrder] = []
        for result in self.symbol_results.values():
            orders.extend(result.orders)
        return orders

    @property
    def trades(self) -> list[TradeRecord]:
        """
        聚合所有品种结果中的成交明细.

        Returns
        -------
        list[TradeRecord]
            当前输出中全部成交明细的扁平列表。
        """
        trades: list[TradeRecord] = []
        for result in self.symbol_results.values():
            trades.extend(result.trades)
        return trades

    @property
    def target_volume(self) -> dict[str, int | float]:
        """
        聚合各品种的目标持仓数量.

        Returns
        -------
        dict[str, int | float]
            仅包含显式目标持仓的品种映射。
        """
        target_volume: dict[str, int | float] = {}
        for symbol, result in self.symbol_results.items():
            if result.target_volume is not None:
                target_volume[symbol] = result.target_volume
        return target_volume

    @property
    def first_ticks(self) -> dict[str, UnifiedPriceData]:
        """
        聚合各品种的首笔行情快照.

        Returns
        -------
        dict[str, UnifiedPriceData]
            品种到首笔行情快照的映射。
        """
        first_ticks: dict[str, UnifiedPriceData] = {}
        for symbol, result in self.symbol_results.items():
            if result.first_tick is not None:
                first_ticks[symbol] = result.first_tick
        return first_ticks

    def get_total_orders(self) -> int:
        """
        返回订单总数.

        Returns
        -------
        int
            当前输出聚合后的订单数量。
        """
        return len(self.orders)

    def get_filled_orders(self) -> list[UnifiedOrder]:
        """
        返回已完成的订单列表.

        Returns
        -------
        list[UnifiedOrder]
            已完成或终态订单列表。
        """
        return [order for order in self.orders if order.is_completed()]

    def get_active_orders(self) -> list[UnifiedOrder]:
        """
        返回当前活跃的订单列表.

        Returns
        -------
        list[UnifiedOrder]
            仍处于活跃状态的订单列表。
        """
        return [order for order in self.orders if order.is_active()]

    def get_orders_by_symbol(self, symbol: str) -> list[UnifiedOrder]:
        """
        返回指定品种的订单列表.

        Parameters
        ----------
        symbol : str
            需要筛选的品种代码。

        Returns
        -------
        list[UnifiedOrder]
            属于指定品种的订单列表。
        """
        return [order for order in self.orders if order.symbol == symbol]

    def get_symbols(self) -> list[str]:
        """
        返回输出中涉及的全部品种代码.

        Returns
        -------
        list[str]
            当前 ``symbol_results`` 中的所有键。
        """
        return list(self.symbol_results.keys())

    def get_total_target_value(self) -> float:
        """
        计算目标总市值.

        Returns
        -------
        float
            根据目标持仓和首笔价格快照估算出的总市值。
        """
        total_value = 0.0
        for symbol, volume in self.target_volume.items():
            if symbol in self.first_ticks:
                price = self.first_ticks[symbol].last_price
                total_value += volume * price
        return total_value

    def has_error(self) -> bool:
        """
        判断当前输出是否处于错误状态.

        Returns
        -------
        bool
            若整体执行不成功则返回 ``True``。
        """
        return not self.success

    def get_error_message(self) -> str | None:
        """
        返回当前输出的错误信息.

        Returns
        -------
        str | None
            显式错误信息或 ``memory`` 中的消息字段。
        """
        if self.error:
            return self.error
        message = self.memory.get("message")
        return message if isinstance(message, str) else None
