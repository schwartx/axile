"""
统一订单与成交模型.

定义订单状态快照与独立成交记录，供执行器、结果聚合层和查询逻辑共享。
"""

from datetime import datetime
from enum import Enum
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, computed_field
from typing_extensions import override


# 辅助函数（替代 lambda）
def _get_now_timestamp() -> float:
    """
    返回当前时间戳.

    Returns
    -------
    float
        当前 Unix 时间戳，单位为秒。
    """
    from axile.executor.algorithms.utils.clock import get_default_clock

    return get_default_clock().time()


def _get_now_iso() -> str:
    """
    返回当前时间的 ISO 格式字符串.

    Returns
    -------
    str
        当前本地时间的 ISO 8601 字符串。
    """
    return datetime.fromtimestamp(_get_now_timestamp()).isoformat()


# 导入现有的交易渠道枚举
from axile.common.trade_channel import TradeChannel  # noqa: E402

# 导入订单状态常量
from axile.executor.constants.order_status import OrderStatus  # noqa: E402
from axile.executor.models.value_conversion import PriceValueConversion  # noqa: E402

# 定义extra字段类型
ExtraData = dict[str, object]


def _order_update_time(data: dict[str, object]) -> str:
    """从旧版显式时间或时间戳恢复订单更新时间。"""
    extra = cast(ExtraData, data["extra"])
    explicit = extra.get("_update_time_str")
    if isinstance(explicit, str):
        return explicit
    return datetime.fromtimestamp(cast(float, data["update_timestamp"])).isoformat()


class OrderDirection(str, Enum):
    """订单方向枚举."""

    BUY = "BUY"  # 买入
    SELL = "SELL"  # 卖出


class OrderType(str, Enum):
    """订单类型枚举."""

    MARKET = "MARKET"  # 市价单
    LIMIT = "LIMIT"  # 限价单


class TradeRecord(BaseModel):
    """
    统一成交记录数据模型.

    Attributes
    ----------
    trade_id : str
        成交记录标识。
    symbol : str
        成交对应的品种代码。
    order_id : str
        成交对应的订单标识。
    trade_time : str
        成交时间。
    trade_volume : float
        成交数量。
    trade_price : float
        成交价格。
    """

    model_config = ConfigDict(
        use_enum_values=True,
        validate_default=False,
        validate_assignment=False,
        extra="ignore",
    )

    trade_id: str = Field(..., description="成交ID")
    symbol: str = Field(default="", description="成交对应的品种代码")
    order_id: str = Field(default="", description="成交对应的订单ID")
    trade_time: str = Field(..., description="成交时间（ISO格式）")
    trade_volume: float = Field(..., description="成交数量")
    trade_price: float = Field(..., description="成交价格")
    trade_value: float = Field(..., description="成交金额")
    value_conversion: PriceValueConversion | None = Field(default=None, description="原生成交金额的审计换算依据")

    # 渠道特有数据
    extra: ExtraData = Field(
        default={},
        description="渠道特有数据，包含原始成交数据和渠道特定字段",
    )

    @override
    def __str__(self) -> str:
        """返回便于记录日志的简要字符串表示."""
        return (
            f"TradeRecord(id={self.trade_id}, symbol={self.symbol}, "
            f"order_id={self.order_id}, volume={self.trade_volume}, price={self.trade_price})"
        )

    @classmethod
    def create(
        cls,
        trade_id: str,
        symbol: str = "",
        order_id: str = "",
        trade_time: str | None = None,
        trade_volume: float = 0.0,
        trade_price: float = 0.0,
        **kwargs: object,
    ) -> "TradeRecord":
        """
        从原始成交字段创建统一成交记录.

        Parameters
        ----------
        trade_id : str
            成交记录标识。
        symbol : str, default=""
            品种代码。
        order_id : str, default=""
            订单标识。
        trade_time : str | None, default=None
            成交时间；为空时使用当前时间。
        trade_volume : float, default=0.0
            成交数量。
        trade_price : float, default=0.0
            成交价格。
        **kwargs : object
            需要保存在 ``extra`` 中的附加字段。

        Returns
        -------
        TradeRecord
            规范化后的成交记录对象。
        """
        if trade_time is None:
            trade_time = _get_now_iso()

        raw_extra = kwargs.get("extra", {})
        extra = dict(cast("ExtraData", raw_extra)) if isinstance(raw_extra, dict) else {}
        for key, value in kwargs.items():
            if key == "extra":
                continue
            extra[key] = value
        if not symbol:
            raw_symbol = extra.get("symbol")
            symbol = str(raw_symbol or "") if raw_symbol is not None else ""
        if not order_id:
            for candidate_key in (
                "order_id",
                "cl_ord_id",
                "order_ref",
                "order_sysid",
                "exchange_order_id",
            ):
                raw_order_id = extra.get(candidate_key)
                if raw_order_id:
                    order_id = str(raw_order_id)
                    break

        trade_value = trade_volume * trade_price

        return cls(
            trade_id=trade_id,
            symbol=symbol,
            order_id=order_id,
            trade_time=trade_time,
            trade_volume=trade_volume,
            trade_price=trade_price,
            trade_value=trade_value,
            extra=extra,
        )


class UnifiedOrder(BaseModel):
    """
    统一订单状态快照模型.

    Attributes
    ----------
    order_id : str
        订单标识。
    symbol : str
        品种代码。
    direction : OrderDirection
        买卖方向。
    order_type : OrderType
        订单类型。
    volume : float
        委托数量。
    price : float
        委托价格。
    status : str
        当前订单状态。
    """

    model_config = ConfigDict(
        use_enum_values=True,
        validate_default=False,
        validate_assignment=False,
        extra="ignore",
    )

    # === 核心字段（所有渠道都有） ===
    order_id: str = Field(..., description="订单ID")
    symbol: str = Field(..., description="品种代码（统一格式）")
    direction: OrderDirection = Field(..., description="买卖方向")
    order_type: OrderType = Field(..., description="订单类型")
    volume: float = Field(..., description="委托数量")
    price: float = Field(..., description="委托价格（市价单时可能为0）")
    status: str = Field(..., description="订单状态")

    # === 成交相关信息 ===
    filled_volume: float = Field(default=0.0, description="成交数量")
    avg_price: float = Field(default=0.0, description="成交均价")
    value_conversion: PriceValueConversion | None = Field(default=None, description="原生成交金额的审计换算依据")

    # === 时间信息 ===
    create_time: str = Field(
        default_factory=_get_now_iso,
        description="创建时间（ISO格式）",
    )
    update_timestamp: float = Field(
        default_factory=_get_now_timestamp,
        description="更新时间戳（秒）",
        exclude=True,  # 序列化时排除
    )

    # === 渠道特有数据 ===
    extra: ExtraData = Field(
        default={},
        description="渠道特有数据，包含原始订单数据和渠道特定字段",
    )
    update_time: str = Field(
        default_factory=_order_update_time,
        description="更新时间（ISO格式，跨进程序列化时保留原值）",
    )

    @override
    def model_post_init(self, context: object) -> None:
        """让兼容时间戳与持久化时间一致，包括跳过校验的构造入口。"""
        try:
            self.update_timestamp = datetime.fromisoformat(self.update_time).timestamp()
        except (ValueError, OverflowError, OSError):
            # 旧渠道允许原生时间文本；解析失败仍保留原文。
            pass

    # === 计算字段（缓存计算结果） ===
    @computed_field
    @property
    def remaining_volume(self) -> float:
        """
        返回剩余未成交数量.

        Returns
        -------
        float
            ``volume`` 扣除 ``filled_volume`` 后的剩余数量。
        """
        return max(0.0, self.volume - self.filled_volume)

    @computed_field
    @property
    def filled_ratio(self) -> float:
        """
        返回当前订单的成交比例.

        Returns
        -------
        float
            范围通常位于 ``0`` 到 ``1`` 之间的成交比例。
        """
        return self.filled_volume / self.volume if self.volume > 0 else 0.0

    @override
    def __str__(self) -> str:
        """返回便于记录日志的简要字符串表示."""
        return (
            f"UnifiedOrder(id={self.order_id}, "
            f"symbol={self.symbol}, {self.direction} {self.volume} "
            f"@ {self.price}, status={self.status})"
        )

    def is_completed(self) -> bool:
        """
        判断订单是否已进入完成态.

        Returns
        -------
        bool
            当订单状态属于成交、撤销、拒绝或过期等终态时返回 ``True``。
        """
        return OrderStatus.is_completed(self.status)

    def is_active(self) -> bool:
        """
        判断订单是否仍处于活跃状态.

        Returns
        -------
        bool
            当订单状态仍允许继续成交或撤单时返回 ``True``。
        """
        return OrderStatus.is_active(self.status)

    @classmethod
    def create(
        cls,
        order_id: str,
        symbol: str,
        direction: str,
        order_type: str,
        volume: float,
        price: float = 0.0,
        channel_type: TradeChannel = TradeChannel("unknown"),
        **kwargs: object,
    ) -> "UnifiedOrder":
        """
        从原始订单字段创建统一订单对象.

        Parameters
        ----------
        order_id : str
            订单标识。
        symbol : str
            品种代码。
        direction : str
            订单方向字符串。
        order_type : str
            订单类型字符串。
        volume : float
            委托数量。
        price : float, default=0.0
            委托价格。
        channel_type : TradeChannel, default="unknown"
            订单来源渠道类型。
        **kwargs : object
            额外订单字段，会被规范化后保存到 ``extra``。

        Returns
        -------
        UnifiedOrder
            规范化后的统一订单对象。
        """
        # 类型转换
        direction_enum = OrderDirection(direction)
        order_type_enum = OrderType(order_type)

        # 确定初始状态（使用 4 字符格式）
        status_obj = kwargs.get("status", OrderStatus.SUBMITTED)
        status = status_obj if isinstance(status_obj, str) else OrderStatus.SUBMITTED

        update_time_str = kwargs.get("update_time", _get_now_iso())

        # 创建订单
        filled_volume_obj = kwargs.get("filled_volume", 0.0)
        avg_price_obj = kwargs.get("avg_price", 0.0)
        create_time_obj = kwargs.get("create_time", _get_now_iso())

        order = cls(
            order_id=order_id,
            symbol=symbol,
            direction=direction_enum,
            order_type=order_type_enum,
            volume=float(volume),
            price=float(price),
            status=status,
            filled_volume=float(filled_volume_obj) if isinstance(filled_volume_obj, (int, float, str)) else 0.0,
            avg_price=float(avg_price_obj) if isinstance(avg_price_obj, (int, float, str)) else 0.0,
            create_time=(create_time_obj if isinstance(create_time_obj, str) else _get_now_iso()),
            update_time=update_time_str if isinstance(update_time_str, str) else _get_now_iso(),
        )

        # 添加渠道特有数据
        order.extra.update(
            {
                "channel_type": channel_type,
                "raw_order_data": kwargs.get("raw_order_data", {}),
            }
        )
        if isinstance(update_time_str, str):
            order.extra["_update_time_str"] = update_time_str

        # 未被统一模型消费的字段全部保留，供渠道插件自行解释。
        reserved_fields = {"status", "filled_volume", "avg_price", "create_time", "update_time", "raw_order_data"}
        order.extra.update({key: value for key, value in kwargs.items() if key not in reserved_fields})

        return order

    def to_dict(self) -> dict[str, object]:
        """
        导出为普通字典.

        Returns
        -------
        dict[str, object]
            适合序列化或日志输出的字典结果。
        """
        return self.model_dump()
