"""GM SDK 操作白名单与调用桥接."""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from gm.api import (
    OrderType_Limit,  # type: ignore  # pyright: ignore[reportUnknownVariableType]
    current,
    get_cash,  # pyright: ignore[reportUnknownVariableType, reportAttributeAccessIssue]
    get_execution_reports,
    get_position,  # pyright: ignore[reportUnknownVariableType, reportAttributeAccessIssue]
    order_cancel,
    order_volume,
)

from axile.channels.gm_sessions import SHANGHAI, check_gm_operation
from axile.executor.algorithms.utils.clock import clock_now

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


def _query_orders(*, unfinished: bool, account_id: str | None) -> list[dict[str, Any]]:
    """保留 SDK 查询结果结构，但不把原生失败吞成空集合或部分集合。"""
    from gm.api._errors import check_gm_status  # type: ignore
    from gm.csdk.c_sdk import py_gmi_get_orders, py_gmi_get_unfinished_orders  # type: ignore
    from gm.model import DictLikeOrder  # type: ignore
    from gm.model.storage import context  # type: ignore
    from gm.pb.account_pb2 import Orders  # type: ignore
    from gm.pb.trade_pb2 import GetOrdersReq, GetUnfinishedOrdersReq  # type: ignore
    from gm.pb_to_dict import protobuf_to_dict  # type: ignore

    method = py_gmi_get_unfinished_orders if unfinished else py_gmi_get_orders
    request_type = GetUnfinishedOrdersReq if unfinished else GetOrdersReq
    accounts = tuple(context.accounts.values())
    if not accounts or account_id is not None and not any(account.id == account_id for account in accounts):
        raise RuntimeError("GM 账户查询范围无法确认，未生成完整委托集合")
    rows = []
    for account in accounts:
        if account_id is not None and account.id != account_id:
            continue
        request = request_type()
        request.account_id = account.id
        status, payload = method(request.SerializeToString())
        check_gm_status(status)
        orders = Orders()
        if payload:
            orders.ParseFromString(payload)
        for order in orders.data:
            row = protobuf_to_dict(order, including_default_value_fields=True, dcls=DictLikeOrder)
            row["origin_module"] = order.properties.get("origin_module")
            row["origin_product"] = order.properties.get("origin_product")
            rows.append(row)
    return rows


def get_orders(account_id: str | None = None) -> list[dict[str, Any]]:
    """查询日内委托，任一账户查询失败时抛出原生 SDK 错误。"""
    return _query_orders(unfinished=False, account_id=account_id)


def get_unfinished_orders(account_id: str | None = None) -> list[dict[str, Any]]:
    """查询未结委托，不能以失败账户缺席的集合确认撤单。"""
    return _query_orders(unfinished=True, account_id=account_id)


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
        context = kwargs.pop("session", None)
        submission_validator = kwargs.pop("submission_validator", None)
        if operation == "order_cancel":
            check_gm_operation(clock_now(tz=SHANGHAI), cancel=True)
        elif operation == "order_volume":
            check_gm_operation(
                clock_now(tz=SHANGHAI), limit=kwargs.get("order_type") == OrderType_Limit, context=context
            )
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
            account_id = kwargs.get("account_id")
            rows = method(account_id=account_id) if operation != "get_execution_reports" else method()
            return rows if account_id is None else [row for row in rows if row.get("account_id") == account_id]
        if operation == "order_volume" and submission_validator is not None:
            submission_validator()
        if operation == "current":
            kwargs["include_call_auction"] = True
        return method(**kwargs)
