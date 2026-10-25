"""GM SDK 操作白名单与调用桥接."""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

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

    @staticmethod
    def execute_request(operation: GMSdkOperation, **kwargs: Any) -> Any:
        """按白名单执行 GM SDK 请求，省略未指定的账户参数。"""
        try:
            method = {
                "get_cash": get_cash,
                "get_position": get_position,
                "current": current,
                "get_orders": get_orders,
                "get_unfinished_orders": get_unfinished_orders,
                "get_execution_reports": get_execution_reports,
                "order_cancel": order_cancel,
                "order_volume": order_volume,
            }[operation]
        except KeyError as exc:
            raise ValueError(f"未知的 GM bridge 操作: {operation}") from exc
        if kwargs.get("account_id") is None:
            kwargs.pop("account_id", None)
        if operation in {"get_orders", "get_unfinished_orders", "get_execution_reports"}:
            rows = method()
            account_id = kwargs.get("account_id")
            return rows if account_id is None else [row for row in rows if row.get("account_id") == account_id]
        return method(**kwargs)
