"""清仓执行场景测试。"""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionArtifactType
from axile.server.business_facts.artifacts import resolve_artifact_content
from axile.server.core.db import SessionLocal
from axile.server.db.models import Account
from axile.server.execution import backend as execution_backend
from axile.server.execution import clear_positions as clear_positions_execution
from axile.server.execution import intents as execution_intents
from axile.server.execution import lifecycle as execution_lifecycle
from axile.server.execution.worker_backend import worker_audit, worker_state
from tests.unit.server._execution_test_support import (
    FakeExecutor,
    FakeWorkerBackendManager,
    WorkerCommandHarness,
    build_account,
    capture_worker_audit_batch,
    noop_account_control_guard,
    noop_append_execution_event,
)


@pytest.fixture(autouse=True)
def _worker_commands(monkeypatch):
    """在 worker 命令边界测试编排，IPC 传输由 manager 用例覆盖。"""
    manager = WorkerCommandHarness()
    monkeypatch.setattr(execution_backend, "get_worker_backend_manager", lambda: manager)
    monkeypatch.setattr(worker_state, "create_executor_instance", lambda *args, **kwargs: FakeExecutor())
    monkeypatch.setattr(worker_state, "build_account_control_guard", noop_account_control_guard)

    async def flush(executor):
        pass

    monkeypatch.setattr(worker_state, "flush_account_control_records", flush)
    monkeypatch.setattr(worker_audit, "append_execution_event_sync", lambda **kwargs: True)
    monkeypatch.setattr(worker_audit, "append_execution_audit_batch_sync", capture_worker_audit_batch())


@pytest.fixture(autouse=True)
def _stub_intent_promotion(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """执行票状态机由 test_execution_intents.py 专门守护；分发层测试只记录 promote 调用。"""
    calls: list[tuple[str, int]] = []

    async def fake_promote(execution_id: str, account_id: int) -> None:
        calls.append((execution_id, account_id))

    monkeypatch.setattr(execution_intents, "promote_intent_to_running", fake_promote)
    return calls


def test_empty_positions_uses_default_algorithm_when_not_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未提供清仓算法时，应回退到默认清仓算法。"""
    account = build_account()
    fake_executor = FakeExecutor()
    captured: dict[str, object] = {}

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        _ = account, raw_input, execution_id
        result = cast("dict[str, object]", cast(object, output).model_dump(mode="json"))
        captured["result"] = result
        return SimpleNamespace(id=102, is_success=1), result

    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account, **kwargs: fake_executor)
    monkeypatch.setattr(worker_state, "build_account_control_guard", noop_account_control_guard)
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    _ = asyncio.run(clear_positions_execution.__empty_positions(account))

    assert fake_executor.empty_kwargs["algorithm"] == {"method": "TARGET-POS-TASK", "params": {}}
    result = captured["result"]
    assert isinstance(result, dict)
    assert result["memory"]["ts"] == "2026-03-11T21:20:13"


def test_clear_positions_request_accepts_database_trade_channel_string() -> None:
    """Text 列读回普通 str 时，清仓审计输入仍应能正常构造。"""
    account = build_account()
    account.trade_channel = cast("TradeChannel", "gm")

    request = clear_positions_execution._build_clear_positions_backend_request(
        account=account,
        algorithm=None,
        execution_id="exec-db-channel-empty-1",
        logger=clear_positions_execution.loguru.logger,
    )

    audit = cast("dict[str, object]", request.empty_kwargs["extra"])["audit"]
    assert cast("dict[str, object]", audit)["channel"] == "gm"


def test_empty_positions_uses_account_level_algorithm_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置了账户级清仓算法时，应优先使用该算法。"""
    account = build_account(
        empty_positions_algorithm={
            "method": "SINGLE-MAKER",
            "params": {"max_wait_seconds": 90},
        }
    )
    fake_executor = FakeExecutor()

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        _ = account, raw_input, execution_id
        return (
            SimpleNamespace(id=103, is_success=1),
            cast("dict[str, object]", cast(object, output).model_dump(mode="json")),
        )

    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account, **kwargs: fake_executor)
    monkeypatch.setattr(worker_state, "build_account_control_guard", noop_account_control_guard)
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    _ = asyncio.run(clear_positions_execution.__empty_positions(account))

    assert fake_executor.empty_kwargs["algorithm"] == {
        "method": "SINGLE-MAKER",
        "params": {"max_wait_seconds": 90},
    }


def test_empty_positions_routes_gm_account_to_worker_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GM 清仓应走 worker manager，而不是主进程内执行 empty_positions。"""
    account = build_account(
        name="gm-live-account",
        market="A股",
        trade_channel=TradeChannel.GM,
        brokerage="gm",
        account_config={
            "account_id": "gm-account",
            "token": "token",
            "serv_addr": "127.0.0.1:7001",
        },
    )
    manager = FakeWorkerBackendManager()
    captured: dict[str, object] = {}

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        _ = account, raw_input, execution_id
        result = cast("dict[str, object]", cast(object, output).model_dump(mode="json"))
        captured["result"] = result
        return SimpleNamespace(id=206, is_success=1), result

    def unexpected_create_executor_instance(_account: Account) -> FakeExecutor:
        raise AssertionError("GM 路径不应在主进程创建执行器")

    async def unexpected_append_execution_event(**_kwargs: object) -> bool:
        raise AssertionError("GM worker 路径不应在主进程写 execution event")

    async def unexpected_append_execution_artifact(**_kwargs: object) -> bool:
        raise AssertionError("GM worker 路径不应在主进程写 execution artifact")

    monkeypatch.setattr(execution_backend, "get_worker_backend_manager", lambda: manager)
    monkeypatch.setattr(worker_state, "create_executor_instance", unexpected_create_executor_instance)
    monkeypatch.setattr(execution_backend, "append_execution_event", unexpected_append_execution_event)
    monkeypatch.setattr(
        worker_audit, "append_execution_audit_batch_sync", lambda _batch: unexpected_append_execution_artifact()
    )
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    _ = asyncio.run(clear_positions_execution.__empty_positions(account, execution_id="exec-gm-worker-empty-1"))

    assert len(manager.empty_calls) == 1
    empty_call = manager.empty_calls[0]
    assert empty_call["execution_id"] == "exec-gm-worker-empty-1"
    assert cast(dict[str, object], empty_call["empty_kwargs"])["algorithm"] == {
        "method": "SINGLE-MAKER",
        "params": {},
    }
    result = captured["result"]
    assert isinstance(result, dict)
    assert result["memory"]["ts"] == "2026-03-11T21:20:13"


def test_empty_positions_routes_ctp_account_to_worker_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CTP 清仓通过常驻 worker 执行。"""
    account = build_account()
    manager = FakeWorkerBackendManager()
    captured: dict[str, object] = {}

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        _ = account, raw_input, execution_id
        result = cast("dict[str, object]", cast(object, output).model_dump(mode="json"))
        captured["result"] = result
        return SimpleNamespace(id=306, is_success=1), result

    def unexpected_create_executor_instance(_account: Account) -> FakeExecutor:
        raise AssertionError("PROCESS 策略下不应走线程执行器")

    monkeypatch.setattr(execution_backend, "get_worker_backend_manager", lambda: manager)
    monkeypatch.setattr(worker_state, "create_executor_instance", unexpected_create_executor_instance)
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    _ = asyncio.run(clear_positions_execution.__empty_positions(account, execution_id="exec-ctp-worker-empty-1"))

    assert len(manager.empty_calls) == 1
    assert cast(Account, manager.empty_calls[0]["account"]).trade_channel == TradeChannel.CTP
    result = captured["result"]
    assert isinstance(result, dict)
    assert result["memory"]["ts"] == "2026-03-11T21:20:13"


def test_empty_positions_wraps_worker_failure_with_error_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 清仓异常时，应保留统一错误消息并写失败审计。"""
    account = build_account()
    captured_event: dict[str, object] = {}
    captured_error_record: dict[str, object] = {}

    class FailingWorkerBackendManager:
        async def empty_positions(self, **_kwargs: object) -> object:
            raise RuntimeError("worker exploded")

    async def fake_append_execution_event(**kwargs: object) -> bool:
        captured_event.update(kwargs)
        return True

    async def fake_append_error_execute_record(**kwargs: object) -> SimpleNamespace:
        captured_error_record.update(kwargs)
        return SimpleNamespace(id=406, is_success=0)

    monkeypatch.setattr(execution_backend, "get_worker_backend_manager", lambda: FailingWorkerBackendManager())
    monkeypatch.setattr(execution_backend, "append_execution_event", fake_append_execution_event)
    monkeypatch.setattr(execution_backend, "append_error_execute_record", fake_append_error_execute_record)

    with pytest.raises(ValueError, match="清仓执行失败，具体原因未确认"):
        asyncio.run(clear_positions_execution.__empty_positions(account, execution_id="exec-empty-worker-failed-1"))

    assert captured_event["execution_id"] == "exec-empty-worker-failed-1"
    assert captured_event["event_type"] == execution_backend.ExecutionEventType.EXECUTION_FAILED
    # 人话错误在公共层；trigger_source 等元数据留在 debug 命名空间。
    details = captured_event["details"]
    assert details["error"] == "清仓执行失败，具体原因未确认"
    assert details["debug"] == {"trigger_source": "empty_positions"}
    assert details["technical_detail"] == "worker exploded"
    assert "RuntimeError: worker exploded" in details["traceback"]
    assert captured_error_record["account_id"] == 1
    assert captured_error_record["execution_id"] == "exec-empty-worker-failed-1"
    assert captured_error_record["raw_result"]["technical_detail"] == "worker exploded"
    assert "RuntimeError: worker exploded" in captured_error_record["raw_result"]["traceback"]


def test_empty_positions_uses_custom_algorithm_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式提供的清仓算法应优先于默认算法。"""
    account = build_account(
        empty_positions_algorithm={
            "method": "SINGLE-MAKER",
            "params": {"max_wait_seconds": 90},
        }
    )
    fake_executor = FakeExecutor()

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        _ = account, raw_input, execution_id
        return (
            SimpleNamespace(id=104, is_success=1),
            cast("dict[str, object]", cast(object, output).model_dump(mode="json")),
        )

    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account, **kwargs: fake_executor)
    monkeypatch.setattr(worker_state, "build_account_control_guard", noop_account_control_guard)
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    asyncio.run(
        clear_positions_execution.__empty_positions(
            account,
            algorithm={
                "method": "SINGLE-MAKER",
                "params": {"max_wait_seconds": 90},
            },
        )
    )

    assert fake_executor.empty_kwargs["algorithm"] == {
        "method": "SINGLE-MAKER",
        "params": {"max_wait_seconds": 90},
    }


def test_empty_positions_binds_account_control_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """empty_positions 入口应绑定账户控制防护层。"""
    account = build_account()
    fake_executor = FakeExecutor()
    fake_guard = object()
    captured: dict[str, object] = {}

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        _ = account, raw_input, execution_id
        return (
            SimpleNamespace(id=202, is_success=1),
            cast("dict[str, object]", cast(object, output).model_dump(mode="json")),
        )

    async def fake_build_account_control_guard(_account: Account, execution_id: str | None, **kwargs) -> object:
        assert kwargs == {"context_kind": "execution", "source": "algorithm"}
        captured["account_id"] = _account.id
        captured["channel"] = _account.trade_channel
        captured["execution_id"] = execution_id
        return fake_guard

    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account, **kwargs: fake_executor)
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)
    monkeypatch.setattr(worker_state, "build_account_control_guard", fake_build_account_control_guard)
    monkeypatch.setattr(
        worker_audit, "append_execution_event_sync", lambda **kw: asyncio.run(noop_append_execution_event(**kw))
    )
    monkeypatch.setattr(worker_audit, "append_execution_audit_batch_sync", capture_worker_audit_batch())

    _ = asyncio.run(clear_positions_execution.__empty_positions(account, execution_id="exec-empty-bind-1"))

    assert captured == {
        "account_id": 1,
        "channel": TradeChannel.CTP,
        "execution_id": "exec-empty-bind-1",
    }
    assert fake_executor.executed_guard is fake_guard
    assert fake_executor.account_control_guard is None


def test_empty_positions_lock_acquired_reuses_tracked_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持锁模式下应复用既有 execution，不再重复注册、标记或释放运行占位。"""
    account = build_account()
    fake_record = SimpleNamespace(id=303, is_success=1)
    captured: dict[str, object] = {}

    async def fake_trade_channel_check(_account: Account, execution_id: str | None = None) -> None:
        return None

    async def fake_inner_empty_positions(
        _account: Account,
        algorithm: dict[str, object] | None = None,
        execution_id: str | None = None,
        logger: object | None = None,
    ) -> SimpleNamespace:
        _ = algorithm, logger
        captured["execution_id"] = execution_id
        return fake_record

    def _forbidden_register(**_kwargs: object) -> str:
        raise AssertionError("持锁模式不应再次注册 execution")

    def _forbidden_clear(*_args: object) -> None:
        raise AssertionError("持锁模式不应释放账户级运行占位")

    async def _forbidden_mark(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("持锁模式不应重复标记 inline execution")

    monkeypatch.setattr(clear_positions_execution, "trade_channel_check", fake_trade_channel_check)
    monkeypatch.setattr(clear_positions_execution, "__empty_positions", fake_inner_empty_positions)
    monkeypatch.setattr(clear_positions_execution, "register_inline_execution", _forbidden_register)
    monkeypatch.setattr(clear_positions_execution, "clear_running_execution", _forbidden_clear)
    monkeypatch.setattr(execution_lifecycle, "mark_inline_execution_succeeded", _forbidden_mark)

    record = asyncio.run(
        clear_positions_execution.empty_positions(
            account,
            execution_id="exec-locked-1",
            lock_acquired=True,
        )
    )

    assert record is fake_record
    assert captured["execution_id"] == "exec-locked-1"


def test_empty_positions_captures_before_account_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清仓应像调仓一样采执行前账户快照并穿进对账.

    否则 ``build_symbol_reconciliation`` 拿到的 ``before`` 恒为 ``None``，``source_before`` 恒记
    ``unavailable``、``equity_before`` 恒缺，前端每次清仓都会误报「基于非真实快照」。
    """
    account = build_account()
    fake_executor = FakeExecutor()
    artifacts: list[dict[str, object]] = []

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        _ = account, raw_input, execution_id
        return (
            SimpleNamespace(id=205, is_success=1),
            cast("dict[str, object]", cast(object, output).model_dump(mode="json")),
        )

    async def capture_artifact(**kwargs: object) -> bool:
        artifacts.append(kwargs)
        return True

    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account, **kwargs: fake_executor)
    monkeypatch.setattr(worker_state, "build_account_control_guard", noop_account_control_guard)
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)
    monkeypatch.setattr(
        worker_audit, "append_execution_event_sync", lambda **kw: asyncio.run(noop_append_execution_event(**kw))
    )
    monkeypatch.setattr(
        worker_audit,
        "append_execution_audit_batch_sync",
        capture_worker_audit_batch(artifact=lambda **kw: asyncio.run(capture_artifact(**kw))),
    )

    asyncio.run(clear_positions_execution.__empty_positions(account, execution_id="exec-empty-before-1"))

    before_artifacts = [a for a in artifacts if a.get("artifact_type") == ExecutionArtifactType.ACCOUNT_SNAPSHOT_BEFORE]
    assert len(before_artifacts) == 1
    content = cast("dict[str, object]", before_artifacts[0]["content"])
    assert content["source"] == "real"
    assert before_artifacts[0]["schema_version"] == 3
    assert "account_assets" not in content
    assert content["business_facts_ref"]["view"] == "assets_before"

    async def read_before_snapshot():
        async with SessionLocal() as session:
            return await resolve_artifact_content(session, before_artifacts[0], account_id=account.id)

    resolved = asyncio.run(read_before_snapshot())
    assert cast("dict[str, object]", resolved["account_assets"])["total_asset"] == 1000.0
    # 前基线确实穿进对账，source_before 不再恒为 unavailable
    summary = next(a["content"] for a in artifacts if a["artifact_type"] == ExecutionArtifactType.EXECUTION_SUMMARY)
    assert summary["reconciliation"]["account"]["source_before"] == "real"
    assert summary["reconciliation"]["account"]["equity_before"] == 1000.0
