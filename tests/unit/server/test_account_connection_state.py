"""使用真实 SQLite 验证最新状态、配置失效与跨进程观察去重。"""

import threading
import time
from dataclasses import asdict
from multiprocessing import Pipe

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel, select

from axile.executor.connection_state import ConnectionMonitor, ConnectionObservation, connection_observer_scope
from axile.server import connection_state as service
from axile.server.db.models import Account
from axile.server.db.models.account_connection import AccountConnectionState
from axile.server.error_details import exception_details
from axile.server.execution.worker_backend.worker_responses import _build_error_payload
from axile.server.portfolio_function import PortfolioFunctionResult, portfolio_result_from_exception
from tests.unit.server._execution_test_support import build_account


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'connection.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(service, "connection_engine", lambda: engine)
    with Session(engine) as session:
        session.add(build_account(portfolio_id=None))
        session.commit()
        account = session.get(Account, 1)
        account.account_config
        session.expunge_all()
    yield engine, account
    engine.dispose()


def test_single_row_overwrite_recovery_and_restart(database):
    engine, account = database
    binding = service.begin_connection_instance(account)
    assert binding is not None
    collector = service.ConnectionCollector(binding, persist=True)
    with connection_observer_scope(collector.observe):
        monitor = ConnectionMonitor()
    monitor.failed(RuntimeError("旧错误"))
    monitor.failed(RuntimeError("最新错误"))
    with Session(engine) as session:
        rows = session.exec(select(AccountConnectionState)).all()
        assert len(rows) == 1
        assert rows[0].message == "最新错误"
        assert "旧错误" not in str(rows[0].error)
    monitor.succeeded()
    # 独立连接模拟服务重新读取，正常结果不保留任何旧错误。
    with Session(engine) as session:
        current = session.get(Account, 1)
        detail = service.public_connection(current, detail=True)
        assert detail.status == "uninitialized"
        assert detail.last_failure.message == "最新错误"
        assert detail.error is None
        assert detail.message is None
        assert detail.observed_at is None


def test_old_instance_and_out_of_order_snapshots_cannot_overwrite(database):
    engine, account = database
    old = service.begin_connection_instance(account)
    new = service.begin_connection_instance(account)
    failure = ConnectionObservation("error", "2026-10-14T14:47:18", "新故障", {"technical_detail": "1026"})
    service.record_connection_observation(new, 2, failure)
    service.record_connection_observation(old, 99, ConnectionObservation("ok", "2099-01-01"))
    service.record_connection_observation(new, 1, ConnectionObservation("ok", "2099-01-01"))
    service.consume_connection_snapshot({"binding": asdict(new), "sequence": 2, "observation": asdict(failure)})
    with Session(engine) as session:
        assert session.get(AccountConnectionState, 1).message == "新故障"
        assert session.get(AccountConnectionState, 1).sequence == 2


def test_changed_config_rejects_old_runtime_and_clears_error(database):
    engine, account = database
    binding = service.begin_connection_instance(account)
    service.record_connection_observation(binding, 1, ConnectionObservation("error", "now", "旧凭证失效", {}))
    with Session(engine) as session:
        current = session.get(Account, 1)
        current.account_config = {**current.account_config, "password": "replacement"}
        session.add(current)
        session.commit()
        assert service.public_connection(current).status == "uninitialized"
        current.account_config
        session.expunge_all()
    service.record_connection_observation(binding, 2, ConnectionObservation("ok", "later"))
    assert service.begin_connection_instance(account) is None
    assert service.begin_connection_instance(current) is not None
    with Session(engine) as session:
        row = session.get(AccountConnectionState, 1)
        assert row.status == "unknown" and row.error is None and row.message is None


def test_connection_scope_survives_worker_and_portfolio_wrapping():
    original = RuntimeError('SDK run: {"status": 1026, "message": "更新令牌错误"}')
    ConnectionMonitor().failed(original)
    worker = _build_error_payload(original)
    assert worker.error_scope == "account_connection"
    result = portfolio_result_from_exception(original)
    restored = PortfolioFunctionResult.from_payload(result.to_payload())
    detail = exception_details(restored.error)
    assert detail["error_scope"] == "account_connection"
    assert "1026" in detail["technical_detail"]
    assert "更新令牌错误" in detail["traceback"]
    assert "error_scope" not in exception_details(ValueError("普通脚本错误"))


def test_idle_worker_monitor_persists_connection_without_flow_or_business_request(database, monkeypatch):
    from axile.server.account_control import ipc

    engine, account = database
    collector = service.ConnectionCollector(service.begin_connection_instance(account))
    monkeypatch.setattr(ipc.providers, "snapshot", lambda _account_id: None)
    parent, child = Pipe()
    server = threading.Thread(target=ipc.serve, args=(child, 1, collector.snapshot), daemon=True)
    server.start()
    collector.observe(ConnectionObservation("error", "2026-10-14T14:47:18", "空闲期间断线", {}))
    monitor = ipc.RemoteMonitor(parent, 1)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with Session(engine) as session:
                row = session.get(AccountConnectionState, 1)
                if row.status == "error":
                    assert row.message == "空闲期间断线"
                    break
            time.sleep(0.01)
        else:
            pytest.fail("空闲 worker 连接观察未通过独立监控通路保存")
    finally:
        monitor.close()
        server.join(timeout=2)


def test_connection_detail_route_is_read_only_and_account_dto_contains_summary(database, monkeypatch):
    import asyncio

    from axile.server.api.routes import account_crud

    engine, account = database
    binding = service.begin_connection_instance(account)
    service.record_connection_observation(
        binding, 1, ConnectionObservation("error", "now", "连接失败", {"technical_detail": "1026"})
    )
    with Session(engine) as session:
        current = session.get(Account, 1)
        current.connection_state
        session.expunge_all()

    async def lookup(_session, _id):
        return current

    monkeypatch.setattr(account_crud, "_get_account_or_404", lookup)
    detail = asyncio.run(account_crud.account_connection(None, 1))
    assert detail.last_failure.error["technical_detail"] == "1026"
    summary = account_crud._account_public(current).connection
    assert summary.status == "uninitialized" and summary.last_failure_at == "now"


def test_account_deletion_cascades_latest_state_and_rejects_late_callback(database):
    engine, account = database
    binding = service.begin_connection_instance(account)
    service.record_connection_observation(binding, 1, ConnectionObservation("error", "now", "连接失败", {}))
    with Session(engine) as session:
        session.delete(session.get(Account, 1))
        session.commit()
    service.record_connection_observation(binding, 2, ConnectionObservation("ok", "later"))
    assert service.begin_connection_instance(account) is None
    with Session(engine) as session:
        assert session.get(AccountConnectionState, 1) is None


def test_direct_and_worker_paths_keep_custom_snapshot_and_lightweight_dtos(database):
    import asyncio

    engine, account = database
    with service.report_connection_for_account(account):
        monitor = ConnectionMonitor()
    content = {
        "title": "测试渠道自由详情",
        "sections": [{"fields": [{"label": "本次事实", "value": "断开"}], "steps": ["检查目标"]}],
    }
    monitor.failed(RuntimeError("SDK 原始错误"), label="此前未注册的自由文案", presentation=content)
    with Session(engine) as session:
        current = session.get(Account, 1)
        summary = service.public_connection(current).model_dump()
        detail = service.public_connection(current, detail=True).model_dump()
        assert summary["label"] is None
        assert "error" not in summary and "presentation" not in summary
        assert detail["last_failure"]["presentation"]["title"] == "测试渠道自由详情"
        assert detail["last_failure"]["error"]["technical_detail"] == "SDK 原始错误"
    # worker 业务响应与独立监控使用同一个 collector 载荷。
    worker = service.ConnectionCollector(service.begin_connection_instance(account))
    observation = ConnectionObservation("error", "2026-10-14T14:47:18", "worker故障", {}, "worker 自由文案", content)
    worker.observe(observation)
    first = worker.snapshot()
    worker.observe(observation)
    assert worker.snapshot() == first
    service.consume_connection_snapshot(first)
    service.consume_connection_snapshot(first)
    with Session(engine) as session:
        row = session.get(AccountConnectionState, 1)
        assert row.label == "worker 自由文案" and row.presentation["title"] == "测试渠道自由详情"
        assert row.sequence == 1 and row.observed_at == observation.observed_at
    # 同配置重连仍未成功，保留上一故障；新实例和序号保护整份内容。
    newer = service.begin_connection_instance(account)
    service.consume_connection_snapshot(first)
    with Session(engine) as session:
        assert session.get(AccountConnectionState, 1).label == "worker 自由文案"
    service.record_connection_observation(newer, 1, ConnectionObservation("error", "later", "未知新故障", {}))
    with Session(engine) as session:
        row = session.get(AccountConnectionState, 1)
        assert row.label is None and row.presentation is None
    service.record_connection_observation(newer, 2, observation)
    # 用真实异步配置重置入口验证四个故障字段同时清除。
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    async def reset():
        async_engine = create_async_engine(str(engine.url).replace("sqlite:", "sqlite+aiosqlite:"))
        async with AsyncSession(async_engine) as session:
            current = await session.get(Account, 1)
            await session.refresh(current, ["settings"])
            current.account_config = {**current.account_config, "password": "new"}
            await service.reset_connection_for_config(session, current)
            await session.commit()
        await async_engine.dispose()

    asyncio.run(reset())
    with Session(engine) as session:
        row = session.get(AccountConnectionState, 1)
        assert row.status == "unknown"
        assert all(getattr(row, field) is None for field in ("label", "presentation", "message", "error"))


def test_idle_monitor_preserves_presentation_and_rejects_old_configuration(database, monkeypatch):
    from axile.server.account_control import ipc

    engine, account = database
    collector = service.ConnectionCollector(service.begin_connection_instance(account))
    monkeypatch.setattr(ipc.providers, "snapshot", lambda _account_id: None)
    parent, child = Pipe()
    server = threading.Thread(target=ipc.serve, args=(child, 1, collector.snapshot), daemon=True)
    server.start()
    collector.observe(
        ConnectionObservation("error", "confirmed", "空闲断线", {}, "自定义空闲故障", {"title": "空闲期间观察"})
    )
    monitor = ipc.RemoteMonitor(parent, 1)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with Session(engine) as session:
                row = session.get(AccountConnectionState, 1)
                if row.label == "自定义空闲故障":
                    assert row.presentation["title"] == "空闲期间观察"
                    assert row.observed_at == "confirmed"
                    break
            time.sleep(0.01)
        else:
            pytest.fail("独立监控未保存展示内容")
    finally:
        monitor.close()
        server.join(timeout=2)


def test_invalid_stored_presentation_cannot_hide_connection_detail(database):
    engine, account = database
    binding = service.begin_connection_instance(account)
    service.record_connection_observation(
        binding,
        1,
        ConnectionObservation("error", "confirmed", "原始故障", {"technical_detail": "完整证据"}, "有效标签"),
    )
    with Session(engine) as session:
        row = session.get(AccountConnectionState, 1)
        row.presentation = {"title": 123, "sections": "invalid"}
        session.add(row)
        session.commit()
    with Session(engine) as session:
        detail = service.public_connection(session.get(Account, 1), detail=True)
        assert detail.presentation is None
        assert detail.last_failure.label == "有效标签" and detail.last_failure.error["technical_detail"] == "完整证据"


def test_current_session_requires_live_worker_and_host_reconciliation(database, monkeypatch):
    from axile.server import connection_runtime as runtime

    engine, account = database
    alive, now = [True], [0.0]
    monkeypatch.setattr(runtime, "clock_monotonic", lambda: now[0])
    binding = service.begin_connection_instance(account)
    runtime.register_session(binding, lambda: alive[0])
    collector = service.ConnectionCollector(binding)
    collector.observe_session(ConnectionObservation("connecting", "start"))
    collector.observe(ConnectionObservation("ok", "sdk-initialized"))
    service.consume_connection_snapshot(collector.snapshot())
    with Session(engine) as db:
        detail = service.public_connection(db.get(Account, 1), detail=True)
        assert detail.status == "connecting"
        assert detail.last_success_at is None
    collector.observe(ConnectionObservation("ok", "account-read", kind="access"))
    collector.observe_session(ConnectionObservation("ok", "reconciled"))
    service.consume_connection_snapshot(collector.snapshot())
    with Session(engine) as db:
        detail = service.public_connection(db.get(Account, 1), detail=True)
        assert detail.status == "ready" and detail.worker_alive
        assert detail.last_success_at == "account-read"
    now[0] = 6
    with Session(engine) as db:
        assert service.public_connection(db.get(Account, 1)).status == "unknown"
    # Heartbeat repeats the same evidence, restoring freshness without inventing access success.
    service.consume_connection_snapshot(collector.snapshot())
    with Session(engine) as db:
        detail = service.public_connection(db.get(Account, 1))
        assert detail.status == "ready" and detail.last_success_at == "account-read"
    alive[0] = False
    with Session(engine) as db:
        detail = service.public_connection(db.get(Account, 1))
        assert detail.status == "disconnected" and not detail.worker_alive


def test_recovered_history_survives_success_and_does_not_advertise_old_failure(database):
    from axile.server import connection_runtime as runtime

    engine, account = database
    binding = service.begin_connection_instance(account)
    runtime.register_session(binding, lambda: True)
    collector = service.ConnectionCollector(binding)
    collector.observe_session(ConnectionObservation("connecting", "start"))
    collector.observe(ConnectionObservation("error", "failure", "native timeout", {"technical_detail": "evidence"}))
    # An intermediate failure must survive even if monitoring first sees the successful final frame.
    collector.observe(ConnectionObservation("ok", "access", kind="access"))
    collector.observe_session(ConnectionObservation("ok", "ready"))
    service.consume_connection_snapshot(collector.snapshot())
    with Session(engine) as db:
        detail = service.public_connection(db.get(Account, 1), detail=True)
        assert detail.status == "ready" and detail.error is None
        assert detail.last_success_at == "access"
        assert detail.last_failure.message == "native timeout"
    collector.observe(ConnectionObservation("error", "disconnect", "trade disconnected", {}))
    service.consume_connection_snapshot(collector.snapshot())
    with Session(engine) as db:
        detail = service.public_connection(db.get(Account, 1), detail=True)
        assert detail.status == "unavailable" and detail.message == "trade disconnected"
    collector.observe(ConnectionObservation("ok", "native-login"))
    service.consume_connection_snapshot(collector.snapshot())
    with Session(engine) as db:
        assert service.public_connection(db.get(Account, 1)).status == "unavailable"


def test_timeout_ends_session_rejects_late_result_and_preserves_original_failure(database):
    from axile.server import connection_runtime as runtime

    engine, account = database
    binding = service.begin_connection_instance(account)
    runtime.register_session(binding, lambda: True)
    collector = service.ConnectionCollector(binding)
    collector.observe_session(ConnectionObservation("connecting", "start"))
    service.consume_connection_snapshot(collector.snapshot())
    service.end_connection_instance(binding, "连接准备超时", TimeoutError("original timeout"))
    collector.observe_session(ConnectionObservation("ok", "late success"))
    service.consume_connection_snapshot(collector.snapshot())
    with Session(engine) as db:
        detail = service.public_connection(db.get(Account, 1), detail=True)
        assert detail.status == "unavailable" and not detail.worker_alive
        assert detail.retry_after_seconds > 0
        assert detail.last_failure.error["technical_detail"] == "original timeout"
        assert db.get(AccountConnectionState, 1).instance_id == ""
    newer = service.begin_connection_instance(account)
    runtime.register_session(newer, lambda: True)
    service.end_connection_instance(binding, "late old worker exit")
    assert runtime.current_session(1, newer.config_key).status == "uninitialized"
    with Session(engine) as db:
        assert db.get(AccountConnectionState, 1).instance_id == newer.instance_id


def test_active_business_failure_does_not_change_connection(database):
    from axile.server import connection_runtime as runtime

    engine, account = database
    binding = service.begin_connection_instance(account)
    runtime.register_session(binding, lambda: True)
    collector = service.ConnectionCollector(binding)
    collector.observe_session(ConnectionObservation("ok", "ready"))
    service.consume_connection_snapshot(collector.snapshot())
    # No connection event is published for a plain business rejection.
    assert "error_scope" not in exception_details(ValueError("余额不足"))
    with Session(engine) as db:
        assert service.public_connection(db.get(Account, 1)).status == "ready"


def test_restart_revokes_old_instance_but_retains_history_and_manual_disconnect(database, monkeypatch):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from axile.server import connection_runtime as runtime
    from axile.server.core import db as db_module

    engine, account = database
    binding = service.begin_connection_instance(account)
    runtime.register_session(binding, lambda: True)
    collector = service.ConnectionCollector(binding)
    collector.observe(ConnectionObservation("ok", "last-access", kind="access"))
    collector.observe_session(ConnectionObservation("disconnected", "manual-stop"))
    service.consume_connection_snapshot(collector.snapshot())

    async def restart():
        async_engine = create_async_engine(str(engine.url).replace("sqlite:", "sqlite+aiosqlite:"))
        monkeypatch.setattr(db_module, "SessionLocal", async_sessionmaker(async_engine))
        try:
            await service.recover_connection_sessions()
        finally:
            await async_engine.dispose()

    asyncio.run(restart())
    collector.observe_session(ConnectionObservation("ok", "late-old-worker"))
    service.consume_connection_snapshot(collector.snapshot())
    with Session(engine) as db:
        detail = service.public_connection(db.get(Account, 1), detail=True)
        assert detail.status == "disconnected" and not detail.worker_alive
        assert detail.last_success_at == "last-access" and detail.manually_disconnected
    newer = service.begin_connection_instance(account)
    assert newer.manually_disconnected


def test_cooldown_elapsed_time_does_not_reset_with_heartbeats(database, monkeypatch):
    from axile.server import connection_runtime as runtime

    _, account = database
    now = [0.0]
    monkeypatch.setattr(runtime, "clock_monotonic", lambda: now[0])
    binding = service.begin_connection_instance(account)
    runtime.register_session(binding, lambda: True)
    collector = service.ConnectionCollector(binding)
    collector.observe_session(ConnectionObservation("cooldown", "failure", error={"retry_after_seconds": 30}))
    service.consume_connection_snapshot(collector.snapshot())
    assert runtime.current_session(1, binding.config_key).retry_after_seconds == 30
    now[0] = 10
    service.consume_connection_snapshot(collector.snapshot())
    assert runtime.current_session(1, binding.config_key).retry_after_seconds == 20


def _offline_session_worker(*args):
    """真实子进程运行协议，执行器和账户风控使用离线替身。"""
    from axile.executor.connection_state import connection_monitor
    from axile.server.execution.worker_backend import worker_state
    from axile.server.execution.worker_backend.worker import run_worker_backend_loop
    from tests.unit.server._execution_test_support import FakeExecutor

    class OfflineExecutor(FakeExecutor):
        def get_account_assets(self):
            with connection_monitor(self).accessing():
                return super().get_account_assets()

        def get_pending_orders(self, symbol=None):
            return []

    async def noop(*args, **kwargs):
        return None

    worker_state.create_executor_instance = lambda *args, **kwargs: OfflineExecutor()
    worker_state.build_account_control_guard = noop
    worker_state.flush_account_control_records = noop
    run_worker_backend_loop(*args)


def test_real_process_session_reuse_disconnect_and_reconnect_without_broker(database, monkeypatch):
    import asyncio

    from axile.server.execution.resident_connection import AccountConnectionUnavailable
    from axile.server.execution.worker_backend import manager as manager_module
    from axile.server.execution.worker_backend.manager import WorkerBackendManager

    engine, account = database
    monkeypatch.setattr(manager_module, "run_worker_backend_loop", _offline_session_worker)
    manager = WorkerBackendManager()
    snapshot = account.snapshot()

    def status():
        with Session(engine) as db:
            return service.public_connection(db.get(Account, 1), detail=True)

    async def run():
        await manager.prepare_account(snapshot)
        first = manager._workers[1].process.pid
        assert status().status == "ready"
        await manager.get_account_assets(snapshot)
        assert manager._workers[1].process.pid == first
        assert status().last_success_at is not None
        await manager.disconnect_account(snapshot)
        assert status().status == "disconnected" and status().manually_disconnected
        await manager.drop_account(1)
        # 模拟服务重启：新的 worker 仍须继承数据库中保存的主动断开意愿。
        from axile.server.connection_runtime import clear_sessions

        clear_sessions()
        with pytest.raises(AccountConnectionUnavailable, match="请先连接账户"):
            await manager.get_account_assets(snapshot)
        assert not manager._workers
        assert status().status == "disconnected"
        await manager.prepare_account(snapshot, force_reconnect=True)
        assert status().status == "ready" and not status().manually_disconnected
        await manager.drop_account(1)
        assert status().status == "disconnected"

    try:
        asyncio.run(run())
    finally:
        manager.close()


def test_monitor_failure_with_live_process_becomes_unknown_without_ending_sdk(database, monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from axile.server import connection_runtime as runtime
    from axile.server.account_control.ipc import RemoteMonitor

    _, account = database
    now = [0.0]
    monkeypatch.setattr(runtime, "clock_monotonic", lambda: now[0])
    binding = service.begin_connection_instance(account)
    runtime.register_session(binding, lambda: True)
    runtime.observe_session(binding, 1, ConnectionObservation("ok", "ready", kind="session"))

    def broken(*args):
        raise EOFError()

    monitor = RemoteMonitor.__new__(RemoteMonitor)
    monitor.stop = Event()
    monitor.connection = SimpleNamespace(send=broken, close=lambda: None)
    monitor.connection_binding = binding
    monitor.process_alive = lambda: True
    monitor._run()
    assert monitor.failed
    assert runtime.current_session(1, binding.config_key).status == "ready"
    now[0] = 6
    assert runtime.current_session(1, binding.config_key).status == "unknown"
    runtime.observe_session(binding, 2, ConnectionObservation("ok", "business-response", kind="session"))
    assert runtime.current_session(1, binding.config_key).status == "ready"
    runtime.forget_session(1)
    assert runtime.current_session(1, binding.config_key).status == "uninitialized"


def test_concurrent_first_requests_share_one_worker_observation_identity(database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from axile.server.execution.worker_backend.manager import WorkerBackendManager
    from axile.server.execution.worker_backend.protocol import WorkerBackendRequest

    _, account = database
    original = service.begin_connection_instance
    calls = []

    def begin(snapshot):
        calls.append(snapshot.id)
        return original(snapshot)

    monkeypatch.setattr(service, "begin_connection_instance", begin)
    handle = SimpleNamespace(
        connection_binding=None, monitor=None, retired=False, process=SimpleNamespace(is_alive=lambda: True)
    )
    barrier = threading.Barrier(2)

    def bind(index):
        request = WorkerBackendRequest(str(index), "prepare", account.snapshot().model_dump(mode="json"), None, {})
        barrier.wait(timeout=2)
        WorkerBackendManager._bind_connection_request(handle, request)
        return request.connection_binding

    with ThreadPoolExecutor(max_workers=2) as pool:
        bindings = list(pool.map(bind, [1, 2]))
    assert bindings[0] == bindings[1]
    assert calls == [1]


def test_host_cooldown_keeps_channel_presentation_and_original_evidence(database):
    from axile.server import connection_runtime as runtime
    from axile.server.execution import resident_connection as resident

    engine, account = database
    binding = service.begin_connection_instance(account)
    runtime.register_session(binding, lambda: True)
    collector = service.ConnectionCollector(binding)
    original = TimeoutError("native timeout")
    content = {"title": "渠道明确的故障", "sections": [{"fields": [{"label": "原因", "value": "访问超时"}]}]}
    with connection_observer_scope(collector.observe):
        monitor = ConnectionMonitor()
    monitor.failed(original, label="渠道原始标签", presentation=content)
    from axile.executor.connection_state import preserve_connection_failure

    wrapped = RuntimeError("host wrapper")
    preserve_connection_failure(wrapped, original)
    state = resident.ResidentConnectionState(account_id=1, connection_observer=collector.observe_session)
    resident._failed(state, wrapped)
    service.consume_connection_snapshot(collector.snapshot())
    with Session(engine) as db:
        detail = service.public_connection(db.get(Account, 1), detail=True)
        assert detail.status == "unavailable"
        assert detail.label == "渠道原始标签"
        assert detail.presentation.title == "渠道明确的故障"
        assert detail.last_failure.presentation.title == "渠道明确的故障"
        assert detail.error["technical_detail"] == "native timeout"
        assert "reporting_stack" in detail.error
        assert "host wrapper" in detail.error["propagation_traceback"]
        assert detail.retry_after_seconds > 0


def test_ended_worker_read_gate_and_recovery_cooldown_survive_process_removal(database, monkeypatch):
    import asyncio

    from axile.server import connection_runtime as runtime
    from axile.server.execution.resident_connection import AccountConnectionUnavailable
    from axile.server.execution.worker_backend.manager import WorkerBackendManager
    from axile.server.execution.worker_backend.protocol import WorkerBackendResponse

    _, account = database
    now = [0.0]
    monkeypatch.setattr(runtime, "clock_monotonic", lambda: now[0])
    binding = service.begin_connection_instance(account)
    runtime.register_session(binding, lambda: True)
    service.end_connection_instance(binding, "worker timeout", TimeoutError("native timeout"))
    manager = WorkerBackendManager()
    requests = []

    def request(*args):
        requests.append(args[1].command)
        return WorkerBackendResponse(request_id=args[1].request_id, kind="result", output_payload={"prepared": True})

    monkeypatch.setattr(manager, "_request_blocking", request)

    async def run():
        for _ in range(2):
            with pytest.raises(AccountConnectionUnavailable, match="请先连接账户"):
                await manager.get_account_assets(account)
            with pytest.raises(AccountConnectionUnavailable, match="请先连接账户"):
                await manager.order_command(account, {"command": "query"})
        with pytest.raises(AccountConnectionUnavailable, match="冷却") as error:
            await manager.prepare_account(account)
        assert error.value.retry_after == 30
        assert requests == [] and not manager._workers
        await manager.prepare_account(account, force_reconnect=True)
        now[0] = 31
        # 自动执行准备在冷却后允许恢复；页面读取仍不能隐式恢复旧会话。
        with pytest.raises(AccountConnectionUnavailable):
            await manager.get_account_assets(account)
        await manager.prepare_account(account)
        assert requests == ["prepare", "prepare"]

    try:
        asyncio.run(run())
    finally:
        manager.close()


def test_logout_snapshot_preserves_evidence_without_manual_disconnect(database):
    engine, account = database
    binding = service.begin_connection_instance(account)
    collector = service.ConnectionCollector(binding)
    collector.observe_session(ConnectionObservation("ok", "ready"))
    collector.observe(
        ConnectionObservation(
            "error",
            "logout",
            "CTP 交易会话已登出",
            {"reason_code": "CTP_USER_LOGOUT", "technical_detail": "OnRspUserLogout request_id=42"},
            "交易会话已登出",
            {"title": "交易会话已登出", "summary": "当前账户会话已失效。"},
        )
    )
    service.consume_connection_snapshot(collector.snapshot())
    with Session(engine) as session:
        row = session.get(AccountConnectionState, account.id)
        assert not row.manually_disconnected
        assert row.last_failure["error"]["reason_code"] == "CTP_USER_LOGOUT"
        assert row.last_failure["presentation"]["title"] == "交易会话已登出"
    collector.observe_session(ConnectionObservation("ok", "recovered"))
    service.consume_connection_snapshot(collector.snapshot())
    with Session(engine) as session:
        row = session.get(AccountConnectionState, account.id)
        assert row.error is None
        assert row.last_failure["observed_at"] == "logout"
