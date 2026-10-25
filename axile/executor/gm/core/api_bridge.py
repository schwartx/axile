"""GM SDK 操作白名单与调用桥接."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Literal, TypeAlias

from gm.api import (  # type: ignore  # pyright: ignore[reportUnknownVariableType]
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
    def execute_request(cls, operation: GMSdkOperation, **kwargs: Any) -> Any:
        """按白名单执行 GM SDK 请求，省略未指定的账户参数。"""
        try:
            method = cls._SDK_OPERATION_METHODS[operation]
        except KeyError as exc:
            raise ValueError(f"未知的 GM bridge 操作: {operation}") from exc
        if kwargs.get("account_id") is None:
            kwargs.pop("account_id", None)
        return method(**kwargs)

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
