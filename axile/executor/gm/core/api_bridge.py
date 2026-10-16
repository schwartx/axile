"""GM SDK 调用桥接与类型化请求定义."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, ClassVar, Literal, TypeAlias

from gm.api import (  # type: ignore  # pyright: ignore[reportUnknownVariableType]
    OrderSide_Buy,
    OrderSide_Sell,
    OrderType_Limit,
    OrderType_Market,
    PositionEffect_Close,
    PositionEffect_Open,
    current,
    get_cash,  # pyright: ignore[reportUnknownVariableType, reportAttributeAccessIssue]
    get_execution_reports,
    get_orders,
    get_position,  # pyright: ignore[reportUnknownVariableType, reportAttributeAccessIssue]
    get_unfinished_orders,
    order_cancel,
    order_volume,
)

GMSdkOperation: TypeAlias = Literal[
    "get_cash",
    "get_position",
    "current",
    "get_orders",
    "get_unfinished_orders",
    "get_execution_reports",
    "order_cancel",
    "order_volume",
]

GMBridgeOperation: TypeAlias = GMSdkOperation | Literal["subscribe_symbols"]


class GMOrderSide(IntEnum):
    """
    描述 GM 下单方向的桥接枚举.

    Attributes
    ----------
    BUY : int
        买入方向。
    SELL : int
        卖出方向。
    """

    BUY = OrderSide_Buy
    SELL = OrderSide_Sell


class GMOrderKind(IntEnum):
    """
    描述 GM 委托价格类型的桥接枚举.

    Attributes
    ----------
    LIMIT : int
        限价单。
    MARKET : int
        市价单。
    """

    LIMIT = OrderType_Limit
    MARKET = OrderType_Market


class GMPositionEffect(IntEnum):
    """
    描述 GM 开平仓语义的桥接枚举.

    Attributes
    ----------
    OPEN : int
        开仓。
    CLOSE : int
        平仓。
    """

    OPEN = PositionEffect_Open
    CLOSE = PositionEffect_Close


@dataclass(slots=True, frozen=True)
class GetCashRequest:
    """
    描述 GM ``get_cash`` 调用的请求参数.

    Attributes
    ----------
    account_id : str | None
        可选的账户标识；为空时使用 GM 当前默认账户。
    """

    account_id: str | None = None

    operation: ClassVar[GMSdkOperation] = "get_cash"

    def as_kwargs(self) -> dict[str, Any]:
        """返回 ``get_cash`` 请求的关键字参数."""
        return {"account_id": self.account_id} if self.account_id is not None else {}


@dataclass(slots=True, frozen=True)
class GetPositionRequest:
    """
    描述 GM ``get_position`` 调用的请求参数.

    Attributes
    ----------
    account_id : str | None
        可选的账户标识；为空时使用 GM 当前默认账户。
    """

    account_id: str | None = None

    operation: ClassVar[GMSdkOperation] = "get_position"

    def as_kwargs(self) -> dict[str, Any]:
        """返回 ``get_position`` 请求的关键字参数."""
        return {"account_id": self.account_id} if self.account_id is not None else {}


@dataclass(slots=True, frozen=True)
class CurrentRequest:
    """
    描述 GM ``current`` 行情查询请求.

    Attributes
    ----------
    symbols : list[str]
        需要订阅或查询的标的代码列表。
    """

    symbols: list[str]

    operation: ClassVar[GMSdkOperation] = "current"

    def as_kwargs(self) -> dict[str, Any]:
        """返回 ``current`` 请求的关键字参数."""
        return {"symbols": self.symbols}


@dataclass(slots=True, frozen=True)
class GetOrdersRequest:
    """
    描述 GM ``get_orders`` 查询请求.

    Attributes
    ----------
    account_id : str | None
        可选的账户标识；指定时仅返回该账户的订单。
    """

    account_id: str | None = None

    operation: ClassVar[GMSdkOperation] = "get_orders"

    def as_kwargs(self) -> dict[str, Any]:
        """返回 ``get_orders`` 请求的关键字参数."""
        return {"account_id": self.account_id} if self.account_id is not None else {}


@dataclass(slots=True, frozen=True)
class GetUnfinishedOrdersRequest:
    """
    描述 GM ``get_unfinished_orders`` 查询请求.

    Attributes
    ----------
    account_id : str | None
        可选的账户标识；指定时仅返回该账户的未完成订单。
    """

    account_id: str | None = None

    operation: ClassVar[GMSdkOperation] = "get_unfinished_orders"

    def as_kwargs(self) -> dict[str, Any]:
        """返回 ``get_unfinished_orders`` 请求的关键字参数."""
        return {"account_id": self.account_id} if self.account_id is not None else {}


@dataclass(slots=True, frozen=True)
class GetExecutionReportsRequest:
    """
    描述 GM ``get_execution_reports`` 查询请求.

    Attributes
    ----------
    account_id : str | None
        可选的账户标识；指定时仅返回该账户的成交回报。
    """

    account_id: str | None = None

    operation: ClassVar[GMSdkOperation] = "get_execution_reports"

    def as_kwargs(self) -> dict[str, Any]:
        """返回 ``get_execution_reports`` 请求的关键字参数."""
        return {"account_id": self.account_id} if self.account_id is not None else {}


@dataclass(slots=True, frozen=True)
class GMCancelOrderTarget:
    """
    描述一次 GM 撤单请求中的目标订单.

    Attributes
    ----------
    cl_ord_id : str
        客户端订单标识。
    account_id : str
        订单所属账户标识。
    """

    cl_ord_id: str
    account_id: str


@dataclass(slots=True, frozen=True)
class OrderCancelRequest:
    """
    描述 GM ``order_cancel`` 调用的请求参数.

    Attributes
    ----------
    wait_cancel_orders : list[GMCancelOrderTarget]
        需要撤销的目标订单列表。
    """

    wait_cancel_orders: list[GMCancelOrderTarget]

    operation: ClassVar[GMSdkOperation] = "order_cancel"

    def as_kwargs(self) -> dict[str, Any]:
        """返回 ``order_cancel`` 请求的关键字参数."""
        return {
            "wait_cancel_orders": [
                {
                    "cl_ord_id": cancel_order.cl_ord_id,
                    "account_id": cancel_order.account_id,
                }
                for cancel_order in self.wait_cancel_orders
            ]
        }


@dataclass(slots=True, frozen=True)
class OrderVolumeRequest:
    """
    描述 GM ``order_volume`` 下单请求.

    Attributes
    ----------
    symbol : str
        下单标的代码。
    volume : int
        下单数量。
    side : GMOrderSide
        买卖方向。
    order_type : GMOrderKind
        委托价格类型。
    position_effect : GMPositionEffect
        开平仓语义。
    price : float
        报价价格。
    account : str
        下单账户标识。
    """

    symbol: str
    volume: int
    side: GMOrderSide
    order_type: GMOrderKind
    position_effect: GMPositionEffect
    price: float
    account: str

    operation: ClassVar[GMSdkOperation] = "order_volume"

    def as_kwargs(self) -> dict[str, Any]:
        """返回 ``order_volume`` 请求的关键字参数."""
        return {
            "symbol": self.symbol,
            "volume": self.volume,
            "side": int(self.side),
            "order_type": int(self.order_type),
            "position_effect": int(self.position_effect),
            "price": self.price,
            "account": self.account,
        }


@dataclass(slots=True, frozen=True)
class GMSubscribeSymbolsRequest:
    """
    描述 GM bridge 内部的标的订阅请求.

    Attributes
    ----------
    symbols : list[str]
        需要补充订阅的标的代码列表。
    """

    symbols: list[str]

    operation: ClassVar[GMBridgeOperation] = "subscribe_symbols"

    def as_kwargs(self) -> dict[str, Any]:
        """返回内部订阅请求的关键字参数."""
        return {"symbols": self.symbols}


GMSdkRequest: TypeAlias = (
    GetCashRequest
    | GetPositionRequest
    | CurrentRequest
    | GetOrdersRequest
    | GetUnfinishedOrdersRequest
    | GetExecutionReportsRequest
    | OrderCancelRequest
    | OrderVolumeRequest
)

GMBridgeRequestPayload: TypeAlias = GMSdkRequest | GMSubscribeSymbolsRequest


class GMApiBridge:
    """统一封装 GM SDK 查询/交易调用."""

    _SDK_OPERATION_METHODS: ClassVar[dict[GMSdkOperation, Callable[..., Any]]]

    @staticmethod
    def get_account_ids() -> list[str]:
        """查询当前 SDK 可见账户，查询失败时抛出原生错误，不能视为空列表。"""
        from gm.api._errors import check_gm_status  # type: ignore
        from gm.csdk.c_sdk import py_gmi_get_accounts  # type: ignore
        from gm.pb.account_pb2 import Accounts  # type: ignore

        code, payload = py_gmi_get_accounts()
        check_gm_status(code)
        accounts = Accounts()
        if payload:
            accounts.ParseFromString(payload)
        return [account.account_id for account in accounts.data]

    @staticmethod
    def get_account_status(account_id: str) -> dict[str, Any] | None:
        """直接读取指定账户的初始状态，避免依赖尚未发生的状态变化回调。"""
        from gm.api._errors import check_gm_status  # type: ignore
        from gm.csdk.c_sdk import py_gmi_get_account_status  # type: ignore
        from gm.pb.account_pb2 import AccountStatuses  # type: ignore
        from gm.pb.tradegw_service_pb2 import GetAccountStatusesReq  # type: ignore
        from gm.pb_to_dict import protobuf_to_dict  # type: ignore

        request = GetAccountStatusesReq()
        request.account_ids.append(account_id)
        code, payload = py_gmi_get_account_status(request.SerializeToString())
        check_gm_status(code)
        statuses = AccountStatuses()
        if payload:
            statuses.ParseFromString(payload)
        for account in statuses.data:
            if account.account_id == account_id:
                return dict(protobuf_to_dict(account.status, including_default_value_fields=True))
        return None

    @classmethod
    def execute_request(cls, request: GMSdkRequest) -> Any:
        """执行类型化的 GM SDK 请求."""
        try:
            method = cls._SDK_OPERATION_METHODS[request.operation]
        except KeyError as exc:
            raise ValueError(f"未知的 GM bridge 操作: {request.operation}") from exc
        return method(**request.as_kwargs())

    @classmethod
    def get_cash(cls, **kwargs: Any) -> Any:
        """转发执行 GM SDK ``get_cash`` 调用."""
        return get_cash(**kwargs)  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]

    @classmethod
    def get_position(cls, **kwargs: Any) -> Any:
        """转发执行 GM SDK ``get_position`` 调用."""
        return get_position(**kwargs)  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]

    @classmethod
    def current(cls, **kwargs: Any) -> Any:
        """转发执行 GM SDK ``current`` 调用."""
        return current(**kwargs)

    @classmethod
    def get_orders(cls, **kwargs: Any) -> Any:
        """转发执行 GM SDK ``get_orders`` 调用."""
        account_id = kwargs.get("account_id")
        orders = get_orders()
        if account_id is None:
            return orders
        return [order for order in orders if order.get("account_id") == account_id]

    @classmethod
    def get_unfinished_orders(cls, **kwargs: Any) -> Any:
        """转发执行 GM SDK ``get_unfinished_orders`` 调用."""
        account_id = kwargs.get("account_id")
        orders = get_unfinished_orders()
        if account_id is None:
            return orders
        return [order for order in orders if order.get("account_id") == account_id]

    @classmethod
    def get_execution_reports(cls, **kwargs: Any) -> Any:
        """转发执行 GM SDK ``get_execution_reports`` 调用."""
        account_id = kwargs.get("account_id")
        reports = get_execution_reports()
        if account_id is None:
            return reports
        return [report for report in reports if report.get("account_id") == account_id]

    @classmethod
    def order_cancel(cls, **kwargs: Any) -> Any:
        """转发执行 GM SDK ``order_cancel`` 调用."""
        return order_cancel(**kwargs)  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]

    @classmethod
    def order_volume(cls, **kwargs: Any) -> Any:
        """转发执行 GM SDK ``order_volume`` 调用."""
        return order_volume(**kwargs)


GMApiBridge._SDK_OPERATION_METHODS = {
    "get_cash": GMApiBridge.get_cash,
    "get_position": GMApiBridge.get_position,
    "current": GMApiBridge.current,
    "get_orders": GMApiBridge.get_orders,
    "get_unfinished_orders": GMApiBridge.get_unfinished_orders,
    "get_execution_reports": GMApiBridge.get_execution_reports,
    "order_cancel": GMApiBridge.order_cancel,
    "order_volume": GMApiBridge.order_volume,
}
