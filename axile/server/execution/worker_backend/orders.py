"""独立委托管道；查询和撤单不排在调仓命令之后，不自动重放请求。"""

from __future__ import annotations

from multiprocessing.connection import Connection
from threading import Lock
from typing import Any
from uuid import uuid4

from axile.executor.connection_state import connection_observer_scope
from axile.server.account_control import providers
from axile.server.account_orders_runtime import order_control_scope, run_order_command
from axile.server.db.models.account import AccountSnapshot
from axile.server.execution.resident_connection import (
    AccountConnectionUnavailable,
    ensure_resident_connection,
    reconcile_resident_executor,
)


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
                failure = response["error"]
                if isinstance(failure, dict):
                    raise AccountConnectionUnavailable(failure["message"], retry_after=failure.get("retry_after", 0))
                raise RuntimeError(failure)
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
    from axile.server.execution.worker_backend import worker_state

    account = AccountSnapshot.model_validate(payload["account"])
    assert account.id is not None
    # 同一资源锁只保护连接准备、人工操作与收尾，不覆盖算法执行。
    with state.resource_lock:
        busy = state.request_active or state.active_execution_id is not None
        if busy:
            if state.executor is None or state.config_signature != worker_state._config_signature(account):
                raise RuntimeError("账户连接正在准备或配置已变化，请稍后刷新")
        binding = ConnectionBinding(**payload["connection_binding"]) if payload.get("connection_binding") else None
        if binding is not None and state.connection_collector.binding != binding:
            state.connection_collector = ConnectionCollector(binding)
            if binding.manually_disconnected and not state.connection_attempted:
                state.account_id, state.config_signature = account.id, worker_state._config_signature(account)
                state.disconnected = state.connection_attempted = True
        guard = providers.active_guard(account.id)

        def initialize(executor):
            with order_control_scope(executor, account, guard):
                worker_state._initialize_worker_connection(state, executor)

        def reconcile(executor):
            with order_control_scope(executor, account, guard):
                reconcile_resident_executor(executor)

        if busy:
            executor = state.executor
            verify = getattr(executor, "_verify_connection", None)
            if callable(verify) and not verify():
                raise AccountConnectionUnavailable("账户交易连接不可用；执行期间不重建连接，当前委托状态待确认")
        else:
            with connection_observer_scope(state.connection_collector.observe):
                state.connection_observer = state.connection_collector.observe_session
                if state.connection_collector.needs_reconciliation:
                    state.connected_once = False
                executor = ensure_resident_connection(
                    state,
                    account,
                    create=lambda: worker_state.create_executor_instance(account, initialize=False),
                    close=lambda _executor: worker_state._close_state_executor(state),
                    initialize=initialize,
                    prepare=lambda executor: worker_state._configure_executor_resources(state, account, executor),
                    reconcile=reconcile,
                    allow_recovery=False,
                )
        with order_control_scope(executor, account, guard, command=payload["command"]):
            return run_order_command(executor, payload)


def serve_orders(connection: Connection, state: Any) -> None:
    """处理当前委托，异常只影响本次操作，不关闭交易 worker。"""
    from loguru import logger

    try:
        while (payload := connection.recv()) is not None:
            try:
                response = {"result": _command(state, payload)}
            except AccountConnectionUnavailable as exc:
                response = {"error": {"message": str(exc), "retry_after": exc.retry_after}}
            except Exception:
                logger.exception("当前委托操作失败 account_id={}", state.account_id)
                response = {"error": "当前委托操作失败，请刷新核对订单状态"}
            connection.send({"request_id": payload["request_id"], **response})
    except (EOFError, OSError):
        pass
    finally:
        connection.close()
