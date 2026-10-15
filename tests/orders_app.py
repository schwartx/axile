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

_runtime = tempfile.TemporaryDirectory(prefix="axile-orders-browser-")
_path = Path(_runtime.name)
(_path / "config.toml").write_text(
    f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{_path / "test.db"}"\napp_log_dir = "{_path / "logs"}"\n'
)
os.environ["AXILE_CONFIG_TOML"] = str(_path / "config.toml")

from sqlmodel import SQLModel

from axile.executor.account_orders import AccountOrderBook
from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.connection_state import ConnectionObservation
from axile.executor.models.unified_order import UnifiedOrder
from axile.server import account_orders, connection_runtime
from axile.server.account_control.ipc import RemoteMonitor, serve
from axile.server.account_orders_runtime import run_order_command
from axile.server.api.routes.account_orders import router
from axile.server.connection_state import ConnectionBinding, ConnectionCollector, connection_config_key
from axile.server.core.db import SessionLocal, engine
from axile.server.event_stream import stream_response
from axile.server.execution.worker_backend.order_monitor import sample_orders
from axile.server.execution.worker_backend.worker_state import _WorkerBackendState
from tests.unit.server._execution_test_support import build_account

book = AccountOrderBook()
stats = {"queries": 0, "cancels": 0}
account = build_account(id=1, portfolio_id=None)
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
    account_orders=book, get_pending_orders=query, cancel_order=cancel, get_account_control_guard=lambda: None
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
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    async with SessionLocal() as session:
        session.add(account)
        await session.commit()
    account_orders._command = command
    connection_runtime.register_session(binding, lambda: True)
    collector.observe_session(ConnectionObservation("ok", clock_now_iso()))
    book.replace([native_order()], book.begin_sync())
    parent, child = Pipe()
    Thread(target=serve, args=(child, 1, collector.snapshot, lambda: sample_orders(state)), daemon=True).start()
    remote = RemoteMonitor(parent, 1, lambda: True)
    remote.connection_binding = binding
    try:
        yield
    finally:
        remote.close()
        await engine.dispose()


app = FastAPI(lifespan=lifespan)
app.include_router(router, prefix="/api/v1/account")


@app.get("/api/v1/events/stream")
async def events(request: Request):
    return stream_response(request)


@app.post("/api/v1/test/orders/{action}")
async def change(action: str):
    if action == "reset":
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
