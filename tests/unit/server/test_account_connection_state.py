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
        assert detail.status == "ok"
        assert detail.error is None
        assert detail.message is None
        assert detail.observed_at is not None


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
        assert service.public_connection(current).status == "unknown"
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
    assert detail.error["technical_detail"] == "1026"
    summary = account_crud._account_public(current).connection
    assert summary.status == "error" and summary.message == "连接失败"


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
        assert summary["label"] == "此前未注册的自由文案"
        assert "error" not in summary and "presentation" not in summary
        assert detail["presentation"]["title"] == "测试渠道自由详情"
        assert detail["error"]["technical_detail"] == "SDK 原始错误"
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
        assert detail.label == "有效标签" and detail.error["technical_detail"] == "完整证据"
