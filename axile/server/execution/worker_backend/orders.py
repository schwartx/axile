"""独立委托管道；查询和撤单不排在调仓命令之后，不自动重放请求。"""

from __future__ import annotations

from multiprocessing.connection import Connection
from threading import Lock
from typing import Any, cast
from uuid import uuid4

from axile.executor.abstract_executor.base import AbstractExecutor
from axile.server.account_control import providers
from axile.server.account_orders_runtime import order_control_scope, run_order_command
from axile.server.db.models.account import AccountSnapshot
from axile.server.execution.factory import initialize_executor_instance


class OrdersChannel:
    """一次只发送一条请求；超时后必须接收旧响应才能继续。"""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.lock = Lock()
        self.pending: str | None = None

    def request(self, payload: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        """等待有界响应；超时不会杀掉正在交易的 worker，也不会重发撤单。"""
        if not self.lock.acquire(timeout=timeout):
            raise TimeoutError("当前委托通路忙，状态待确认")
        try:
            if self.pending is not None:
                self._receive(timeout)
            self.pending = uuid4().hex
            self.connection.send({**payload, "request_id": self.pending})
            response = self._receive(timeout)
            if response.get("error"):
                raise RuntimeError(response["error"])
            return response["result"]
        finally:
            self.lock.release()

    def _receive(self, timeout: float) -> dict[str, Any]:
        if not self.connection.poll(timeout):
            raise TimeoutError("当前委托请求超时，结果待确认")
        response = self.connection.recv()
        expected, self.pending = self.pending, None
        if not isinstance(response, dict) or response.get("request_id") != expected:
            self.connection.close()
            raise RuntimeError("当前委托响应身份不匹配")
        return response

    def close(self) -> None:
        """释放委托管道，独立于交易和终止控制通路。"""
        self.connection.close()


def _command(state: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from axile.server.connection_state import ConnectionBinding, ConnectionCollector
    from axile.server.execution.worker_backend.worker_state import _config_signature, _resolve_executor

    account = AccountSnapshot.model_validate(payload["account"])
    assert account.id is not None
    # 同一资源锁只保护连接准备、人工操作与收尾，不覆盖算法执行。
    with state.resource_lock:
        busy = state.request_active or state.active_execution_id is not None
        if busy:
            if state.executor is None or state.config_signature != _config_signature(account):
                raise RuntimeError("账户连接正在准备或配置已变化，请稍后刷新")
        binding = ConnectionBinding(**payload["connection_binding"]) if payload.get("connection_binding") else None
        if binding is not None and state.connection_collector.binding != binding:
            state.connection_collector = ConnectionCollector(binding)
        if busy:
            executor = state.executor
        else:
            executor = _resolve_executor(state, account, initialize=False)
        guard = providers.active_guard(account.id)
        with order_control_scope(executor, account, guard):
            verify = getattr(executor, "_verify_connection", None)
            if getattr(executor, "_requires_connection_initialization", False) or (callable(verify) and not verify()):
                initialize_executor_instance(cast(AbstractExecutor, executor))
            return run_order_command(executor, payload)


def serve_orders(connection: Connection, state: Any) -> None:
    """处理当前委托，异常只影响本次操作，不关闭交易 worker。"""
    from loguru import logger

    try:
        while (payload := connection.recv()) is not None:
            try:
                response = {"result": _command(state, payload)}
            except Exception:
                logger.exception("当前委托操作失败 account_id={}", state.account_id)
                response = {"error": "当前委托操作失败，请刷新核对订单状态"}
            connection.send({"request_id": payload["request_id"], **response})
    except (EOFError, OSError):
        pass
    finally:
        connection.close()
