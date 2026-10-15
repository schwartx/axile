"""
统一账户资产与持仓模型.

定义执行器内部通用的账户资产快照结构与单品种持仓结构。
"""

import math
from enum import Enum
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import override

from axile.common.trade_channel import TradeChannel

DEGRADED_SNAPSHOT_SOURCES = frozenset({"assumed", "error", "unavailable"})


def is_degraded_snapshot_source(source: object) -> bool:
    """识别明确降级来源；渠道自定义来源仍可提供资产与持仓事实。"""
    return isinstance(source, str) and source in DEGRADED_SNAPSHOT_SOURCES


# 辅助函数（替代 lambda）
def _get_now_iso() -> str:
    """
    返回当前时间的 ISO 格式字符串.

    Returns
    -------
    str
        当前本地时间的 ISO 8601 字符串。
    """
    from axile.executor.algorithms.utils.clock import clock_now_iso

    return clock_now_iso()


ExtraData = dict[str, Any]


class PositionDirection(str, Enum):
    """持仓方向枚举."""

    LONG = "多头"
    SHORT = "空头"
    NET = "净"


class Position(BaseModel):
    """
    统一持仓数据模型.

    Attributes
    ----------
    symbol : str
        品种代码。
    volume : float
        当前持仓数量。
    available_volume : float
        可用于交易的持仓数量。
    market_value : float | None
        当前持仓市值；缺少有效行情时为 ``None``。
    direction : PositionDirection
        持仓方向。
    """

    symbol: str = Field(..., description="品种代码（统一格式）")
    volume: float = Field(..., description="持仓数量（统一为float类型）")
    available_volume: float = Field(..., description="可用数量")
    market_value: float | None = Field(..., description="持仓市值；缺少有效行情时为 None")
    direction: PositionDirection = Field(..., description="持仓方向")
    avg_price: float | None = Field(None, description="成本价")

    # 渠道特有数据
    extra: ExtraData = Field(
        default={},
        description="渠道特有数据，包含原始数据和渠道特定字段",
    )

    model_config = ConfigDict(
        use_enum_values=True,
        validate_default=False,
        validate_assignment=False,
        extra="ignore",
    )

    @override
    def __str__(self) -> str:
        """返回便于记录日志的简要字符串表示."""
        return f"Position(symbol={self.symbol}, volume={self.volume}, direction={self.direction})"


class UnifiedAccountAssets(BaseModel):
    """
    统一账户资产快照模型.

    Attributes
    ----------
    available_cash : float
        可用资金。
    total_asset : float
        总资产。
    market_value : float | None
        持仓市值；任一在仓品种缺少有效行情时为 ``None``。
    positions : list[Position]
        当前持仓列表。
    currency : str
        账户币种。
    update_time : str
        最近更新时间。
    source : str
        账户读取来源标记：``real`` 真实拉取、``assumed`` 读账户失败后退化为假设权益、
        ``unavailable`` 未取得资产；``error`` 转换异常零值；供下游区分快照可信度，避免用退化数据算出「假的到位度」。
    """

    # === 核心字段 ===
    available_cash: float = Field(..., description="可用资金")
    total_asset: float = Field(..., description="总资产")
    market_value: float | None = Field(..., description="持仓市值；缺少有效行情时为 None")
    positions: list[Position] = Field(default=[], description="持仓列表")

    # === 账户级补充字段 ===
    currency: str = Field(default="CNY", description="币种")
    update_time: str = Field(
        default_factory=_get_now_iso,
        description="更新时间（ISO格式）",
    )
    source: str = Field(
        default="real",
        description="账户读取来源：real 或渠道自定义有效来源；assumed、unavailable、error 为降级来源，不提供持仓与权益事实。",
    )

    # === 渠道特有数据 ===
    extra: ExtraData = Field(
        default={},
        description="渠道特有数据，包含原始账户数据和渠道特定字段",
    )

    model_config = ConfigDict(
        use_enum_values=True,
        validate_default=False,
        validate_assignment=False,
        extra="ignore",
    )

    @classmethod
    def unavailable(cls) -> "UnifiedAccountAssets":
        """构造未取得账户资产的占位值，不代表真实零余额。"""
        return cls(available_cash=0, total_asset=0, market_value=0, source="unavailable")

    @override
    def __str__(self) -> str:
        """返回便于记录日志的简要字符串表示."""
        return (
            f"AccountAssets(cash={self.available_cash:.2f}, "
            f"total={self.total_asset:.2f}, positions={len(self.positions)})"
        )

    def get_position(self, symbol: str) -> Position | None:
        """
        根据品种代码查找持仓.

        Parameters
        ----------
        symbol : str
            待查找的品种代码。

        Returns
        -------
        Position | None
            命中的持仓对象；若不存在则返回 ``None``。
        """
        for pos in self.positions:
            if pos.symbol == symbol:
                return pos
        return None

    def get_total_position_value(self) -> float | None:
        """
        计算全部持仓的总市值.

        Returns
        -------
        float | None
            所有持仓 ``market_value`` 的总和；任一持仓无法估值时为 ``None``。
        """
        if any(pos.market_value is None for pos in self.positions):
            return None
        return sum(pos.market_value for pos in self.positions if pos.market_value is not None)

    def validate_balance(self) -> bool:
        """
        验证账户平衡关系是否成立.

        Returns
        -------
        bool
            GM 股票现金账户使用同一资金快照的可用资金、挂单冻结资金与账户市值对账；
            字段不完整时返回 ``False``。其他渠道保留可用资金加持仓市值的校验。
            本方法仅提供诊断，不重算或覆盖原生权益。
        """
        if self.extra.get("channel_type") == TradeChannel.GM:
            return self._validate_gm_cash_balance()
        if self.market_value is None:
            return False
        calculated_total = self.available_cash + self.market_value
        tolerance = 0.01  # 允许0.01的误差
        return abs(self.total_asset - calculated_total) <= tolerance

    def _validate_gm_cash_balance(self) -> bool:
        """使用同一次 GM 股票资金查询的字段诊断余额关系。"""
        cash = self.extra.get("gm_cash")
        if self.extra.get("total_asset_source") != "gm.cash.nav" or not isinstance(cash, dict):
            return False
        values: list[float] = []
        for field in ("nav", "available", "order_frozen", "market_value"):
            value = cash.get(field)
            if isinstance(value, bool) or not isinstance(value, int | float):
                return False
            number = float(value)
            if not math.isfinite(number):
                return False
            values.append(number)
        nav, available, frozen, market_value = values
        return (
            abs(self.total_asset - nav) <= 0.01
            and abs(self.available_cash - available) <= 0.01
            and abs(nav - (available + frozen + market_value)) <= 0.01
        )

    def update_curr_target(
        self,
        curr_target: dict[str, float] | None,
        forbidden_symbols: list[str] | None = None,
        risk_symbols: list[str] | None = None,
    ) -> dict[str, float]:
        """
        基于当前账户持仓更新目标仓位映射.

        Parameters
        ----------
        curr_target : dict[str, float] | None
            当前目标仓位权重映射。
        forbidden_symbols : list[str] | None, default=None
            禁止交易的品种列表。
        risk_symbols : list[str] | None, default=None
            风险品种列表，命中后其权重会被置为 ``0``。

        Returns
        -------
        dict[str, float]
            根据当前持仓、禁止品种与风险品种调整后的目标仓位映射。
        """
        # 参数初始化
        forbidden_symbols = forbidden_symbols or []
        risk_symbols = risk_symbols or []

        # 处理None输入
        if curr_target is None:
            working_target = {}
        else:
            # 复制原目标，避免修改传入参数
            working_target = curr_target.copy()

        # 获取账户最新持仓，将不存在于目标中的品种权重设置为0
        if self.positions:
            for position in self.positions:
                symbol = position.symbol
                if symbol and symbol not in working_target:
                    working_target[symbol] = 0.0

        # 过滤禁止品种和风险品种
        new_target: dict[str, float] = {}
        for symbol, weight in working_target.items():
            if symbol in forbidden_symbols:
                # 跳过禁止交易的品种
                continue
            elif symbol in risk_symbols:
                # 风险品种设置权重为0
                new_target[symbol] = 0.0
            else:
                # 正常品种保持原权重
                new_target[symbol] = weight

        return new_target

    @classmethod
    def create(
        cls,
        available_cash: float,
        total_asset: float,
        positions_data: list[dict[str, Any]] | list[Position],
        channel_type: TradeChannel = TradeChannel("unknown"),
        currency: str = "CNY",
    ) -> "UnifiedAccountAssets":
        """
        从原始持仓数据创建账户资产快照.

        Parameters
        ----------
        available_cash : float
            可用资金。
        total_asset : float
            总资产。
        positions_data : list[dict[str, Any]] | list[Position]
            持仓数据列表，可以是字典列表或 ``Position`` 对象列表。
        channel_type : TradeChannel, default="unknown"
            持仓来源的交易渠道类型。
        currency : str, default="CNY"
            账户币种。

        Returns
        -------
        UnifiedAccountAssets
            构造完成的统一账户资产对象。
        """
        positions: list[Position] = []
        if not positions_data:
            positions = []
        elif isinstance(positions_data[0], Position):
            positions = cast("list[Position]", positions_data)
        else:
            # 处理字典格式的数据
            positions = []
            for pos_data in positions_data:
                # 创建pos_data的副本避免修改原始数据
                pos_copy = dict(pos_data)

                # 统一volume类型为float
                if isinstance(pos_copy.get("volume"), int):
                    pos_copy["volume"] = float(pos_copy["volume"])
                if isinstance(pos_copy.get("available_volume"), int):
                    pos_copy["available_volume"] = float(pos_copy["available_volume"])

                # 添加渠道类型标识到extra字段
                if "extra" not in pos_copy:
                    pos_copy["extra"] = {}
                if isinstance(pos_copy["extra"], dict):
                    pos_copy["extra"]["channel_type"] = channel_type

                # 使用安全的参数创建Position实例
                positions.append(
                    Position(
                        symbol=str(pos_copy["symbol"]),
                        volume=float(pos_copy["volume"]),
                        available_volume=float(pos_copy["available_volume"]),
                        market_value=(
                            float(pos_copy["market_value"]) if pos_copy.get("market_value") is not None else None
                        ),
                        direction=PositionDirection(pos_copy["direction"]),
                        avg_price=float(pos_copy["avg_price"]) if pos_copy.get("avg_price") is not None else None,
                        extra=pos_copy.get("extra", {}),
                    )
                )

        return cls(
            available_cash=available_cash,
            total_asset=total_asset,
            market_value=(
                sum(pos.market_value for pos in positions if pos.market_value is not None)
                if all(pos.market_value is not None for pos in positions)
                else None
            ),
            positions=positions,
            currency=currency,
            extra={"channel_type": channel_type},
        )
