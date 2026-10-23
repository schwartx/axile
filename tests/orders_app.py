"""委托浏览器验收：真实 HTTP/SSE 与一秒 IPC，渠道使用内存替身。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from multiprocessing import Pipe
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

from fastapi import FastAPI, Request
from pydantic_core import MultiHostUrl

_runtime = tempfile.TemporaryDirectory(prefix="axile-orders-browser-")
_path = Path(_runtime.name)
(_path / "config.toml").write_text(
    f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{_path / "test.db"}"\napp_log_dir = "{_path / "logs"}"\n'
)
os.environ["AXILE_CONFIG_TOML"] = str(_path / "config.toml")

from axile.common.config import settings
from axile.common.trade_channel import TradeChannel
from axile.executor.account_activity import AccountActivityBook
from axile.executor.account_orders import AccountOrderBook
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.connection_state import ConnectionObservation
from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
from axile.server import account_orders, connection_runtime
from axile.server.account_control.ipc import RemoteMonitor, serve
from axile.server.account_orders_runtime import run_order_command
from axile.server.api.routes.account_orders import router
from axile.server.connection_state import ConnectionBinding, ConnectionCollector, connection_config_key
from axile.server.event_stream import stream_response
from axile.server.execution.worker_backend.order_monitor import sample_orders
from axile.server.execution.worker_backend.worker_state import _WorkerBackendState
from axile.server.native_store import native_operation_sync
from tests.unit.server._execution_test_support import build_account

settings.sqlalchemy_database_uri = MultiHostUrl(f"sqlite+aiosqlite:///{_path / 'test.db'}")
settings.app_log_dir = _path / "logs"

book = AccountOrderBook()
stats = {"queries": 0, "cancels": 0}
account = build_account(
    id=1,
    portfolio_id=None,
    name="tq-browser",
    trade_channel=TradeChannel.TQ,
    brokerage="tq",
    account_config={"account_mode": "kq", "tq_username": "fixture", "tq_password": "fixture"},
)
binding = ConnectionBinding(1, connection_config_key(account.trade_channel, account.account_config), "browser-worker")
collector = ConnectionCollector(binding)


def native_order(filled=0, status="待成交"):
    return UnifiedOrder(
        order_id="native-1",
        symbol="rb2610",
        direction="BUY",
        order_type="LIMIT",
        price=3500,
        volume=3,
        filled_volume=filled,
        status=status,
    )


def query(_symbol=None):
    stats["queries"] += 1
    return book.snapshot()["data"]


def cancel(_symbol, _order_id):
    stats["cancels"] += 1
    book.apply(native_order(filled=1, status="已撤销"))
    return True


executor = SimpleNamespace(
    account_orders=book,
    get_pending_orders=query,
    cancel_order=cancel,
    get_account_control_guard=lambda: None,
    channel_type="tq",
    query_recent_orders=lambda symbol: book.activity.snapshot(symbol)["orders"],
    query_recent_trades=lambda symbol: book.activity.snapshot(symbol)["trades"],
)
state = _WorkerBackendState(
    executor=executor, account_id=1, connection_collector=collector, orders_binding=binding, orders_executor=executor
)


async def command(_account, payload):
    result = run_order_command(executor, payload)
    result["instance_id"] = f"{binding.instance_id}:{state.orders_generation}"
    return result


@asynccontextmanager
async def lifespan(_app):
    native_operation_sync("migration_upgrade", {})
    receipt = native_operation_sync(
        "account_create",
        {"account": account.model_dump(mode="json"), "now": clock_now_iso(), "idempotency_key": "orders-browser"},
    )
    assert receipt["account"]["id"] == account.id
    previous_command = account_orders._command
    account_orders._command = command
    connection_runtime.register_session(binding, lambda: True)
    collector.observe_session(ConnectionObservation("ok", clock_now_iso()))
    book.replace([native_order()], book.begin_sync())
    parent, child = Pipe()
    thread = Thread(target=serve, args=(child, 1, collector.snapshot, lambda: sample_orders(state)), daemon=True)
    thread.start()
    remote = RemoteMonitor(parent, 1, lambda: True)
    remote.connection_binding = binding
    try:
        yield
    finally:
        remote.close()
        thread.join(timeout=1)
        connection_runtime.forget_session(account.id)
        account_orders._command = previous_command


app = FastAPI(lifespan=lifespan)
app.include_router(router, prefix="/api/v1/account")


@app.get("/api/v1/events/stream")
async def events(request: Request):
    return stream_response(request)


def position_snapshot(account_id: int) -> dict:
    """提供持仓页签的隔离快照，浏览器检查不连接任何交易渠道。"""
    return {
        "id": 1,
        "account_id": account_id,
        "source": "manual",
        "execution_id": None,
        "created_at": "2026-10-19T10:30:00+08:00",
        "assets": {
            "currency": "CNY",
            "source": "real",
            "update_time": "2026-10-19T10:30:00+08:00",
            "positions": [
                {
                    "symbol": "rb2610",
                    "direction": "多头",
                    "volume": 3,
                    "available_volume": 2,
                    "market_value": 105000,
                    "avg_price": 99999,
                    "extra": {
                        "tq_symbol": "SHFE.rb2610",
                        "position_attributes": {
                            "today_volume": 1,
                            "yesterday_volume": 2,
                            "open_price": 3400,
                            "float_profit": 3000,
                            "position_price": 3450,
                            "position_profit": 1500,
                            "margin": 12000,
                            "frozen": 1,
                        },
                    },
                },
                {
                    "symbol": "ag2612",
                    "direction": "空头",
                    "volume": 1,
                    "available_volume": 1,
                    "market_value": None,
                    "avg_price": 0,
                    "extra": {"position_attributes": {"today_volume": 0, "yesterday_volume": 1, "float_profit": 0}},
                },
            ],
        },
    }


@app.get("/api/v1/account/{account_id}/asset_snapshots")
async def saved_position_snapshots(account_id: int):
    return {"data": [position_snapshot(account_id)], "count": 1}


@app.post("/api/v1/account/{account_id}/assets/refresh")
async def refresh_position_snapshot(account_id: int):
    return position_snapshot(account_id)


@app.post("/api/v1/test/orders/{action}")
async def change(action: str):
    if action == "reset":
        book.activity = AccountActivityBook()
        book.activity.observe_trade(
            TradeRecord.create(
                trade_id="fill-1",
                order_id="native-1",
                symbol="rb2610",
                trade_price=3500,
                trade_volume=1,
                direction="BUY",
                offset="OPEN",
            )
        )
        book.activity.observe_order(
            UnifiedOrder(
                order_id="rejected-1",
                symbol="ag2612",
                direction="SELL",
                order_type="LIMIT",
                volume=1,
                price=100,
                status="已拒绝",
                extra={"last_msg": "保证金不足"},
            )
        )
        with book._lock:
            book._orders.clear()
            book._changed.clear()
        account_orders._states.clear()
        book.replace([native_order()], book.begin_sync())
        collector.observe_session(ConnectionObservation("ok", clock_now_iso()))
        stats.update(queries=0, cancels=0)
    elif action == "partial":
        book.apply(native_order(filled=1, status="部分成交"))
    elif action == "disconnect":
        book.invalidate("模拟渠道断线，委托状态待确认")
        collector.observe_session(ConnectionObservation("error", clock_now_iso(), "模拟渠道断线"))
    elif action == "recover":
        book.replace([native_order(filled=1, status="部分成交")], book.begin_sync())
        collector.observe_session(ConnectionObservation("ok", clock_now_iso()))
    await asyncio.sleep(0)
    return stats


@app.get("/api/v1/test/orders/stats")
async def counts():
    return stats
