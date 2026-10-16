"""正式连接与临时验证共用的账户就绪核对。"""

from collections.abc import Callable

from axile.executor.connection_state import connection_monitor


def reconcile_account_connection(executor: object, terminal_order: Callable | None = None) -> None:
    """资产和委托只查询一次；渠道已完成的对账快照可以复用。"""
    monitor = connection_monitor(executor)
    assets = getattr(executor, "get_account_assets", None)
    orders = getattr(executor, "get_pending_orders", None)
    recovery = getattr(executor, "_recovery_snapshot", None)
    monitor.step("account_assets", "running")
    if callable(assets):
        snapshot = recovery["assets"] if isinstance(recovery, dict) and recovery.get("assets") is not None else assets()
        if getattr(snapshot, "source", None) in {"unavailable", "degraded"}:
            raise ConnectionError("账户连接恢复后尚未取得有效资产快照")
    asset_details = None
    if callable(assets):
        asset_details = {
            "title": "账户资产核对",
            "rows": [
                {"key": label, "value": getattr(snapshot, field, None)}
                for label, field in (
                    ("币种", "currency"),
                    ("总资产", "total_asset"),
                    ("可用资金", "available_cash"),
                    ("数据来源", "source"),
                )
            ],
        }
    monitor.step("account_assets", "passed", asset_details)
    monitor.step("pending_orders", "running")
    book = getattr(executor, "account_orders", None)
    if callable(orders) and (book is None or not book.snapshot()["complete"]):
        orders(None)
    if book is not None and terminal_order is not None:
        for order in book.snapshot()["data"]:
            if not order.is_active():
                terminal = terminal_order(executor, order.symbol, order.order_id)
                if terminal is not None:
                    book.apply(terminal)

    order_details = None
    if book is not None:
        order_details = {
            "title": "委托核对",
            "rows": [
                {"key": "委托记录数", "value": len(book.snapshot()["data"])},
                {"key": "快照完整", "value": book.snapshot()["complete"]},
            ],
        }
    monitor.step("pending_orders", "passed", order_details)
