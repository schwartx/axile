"""调仓执行场景测试。"""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionArtifactType, ExecutionEventStatus, ExecutionEventType
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import ExecutionStatus, UnifiedStandardOutput
from axile.executor.models.unified_price import UnifiedPriceData
from axile.server.db.models import Account
from axile.server.execution import backend as execution_backend
from axile.server.execution import intents as execution_intents
from axile.server.execution import rebalance as rebalance_execution
from axile.server.execution.worker_backend import worker_audit, worker_state
from tests.unit.server._execution_test_support import (
    FakeExecutor,
    FakeWorkerBackendManager,
    WarningLogger,
    WorkerCommandHarness,
    build_account,
    capture_worker_audit_batch,
    noop_account_control_guard,
    noop_append_execution_event,
)


class _VolumeCalcStub:
    logger: WarningLogger

    def __init__(self, logger: WarningLogger) -> None:
        self.logger = logger

    @staticmethod
    def _calculate_generic_volume(
        weight: float,
        price: float,
        account_assets: UnifiedAccountAssets,
        _trade_rule: dict[str, object],
        *,
        symbol: str | None = None,
    ) -> float:
        del symbol
        return weight * account_assets.total_asset / price


@pytest.fixture(autouse=True)
def _worker_commands(monkeypatch) -> dict[str, dict[str, object]]:
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
    frozen_inputs: dict[str, dict[str, object]] = {}

    def persist_input(**kwargs: object) -> str:
        frozen_inputs[cast(str, kwargs["execution_id"])] = cast("dict[str, object]", kwargs["payload"])
        return "input-test"

    monkeypatch.setattr(worker_audit, "persist_pre_execute_input_sync", persist_input)
    monkeypatch.setattr(worker_audit, "persist_post_execute_assets_sync", lambda **kwargs: ("input-test", "asset-test"))
    return frozen_inputs


@pytest.fixture(autouse=True)
def _stub_intent_promotion(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """执行票状态机由 test_execution_intents.py 专门守护；分发层测试只记录 promote 调用。"""
    calls: list[tuple[str, int]] = []

    async def fake_promote(execution_id: str, account_id: int) -> None:
        calls.append((execution_id, account_id))

    monkeypatch.setattr(execution_intents, "promote_intent_to_running", fake_promote)

    async def read(name: str, request: dict[str, object]) -> dict[str, object]:
        assert name == "execution_read" and request["op"] == "last_target"
        return {"present": False, "target": None}

    monkeypatch.setattr(rebalance_execution, "native_operation", read)
    return calls


@pytest.mark.parametrize(
    ("present", "target", "expected"),
    [
        (False, None, {}),
        (True, None, None),
        (True, {}, {}),
        (True, {"short": -0.25, "flat": 0}, {"short": -0.25, "flat": 0}),
    ],
)
def test_last_target_reads_only_selected_input_target(
    monkeypatch: pytest.MonkeyPatch,
    present: bool,
    target: object,
    expected: object,
) -> None:
    """上次目标只读取成功记录关联的目标；缺失与显式空值保持不同。"""
    account = build_account()
    calls: list[tuple[str, object]] = []

    async def read(name: str, request: object) -> object:
        calls.append((name, request))
        return {"present": present, "target": target}

    monkeypatch.setattr(rebalance_execution, "native_operation", read)
    assert asyncio.run(rebalance_execution._load_last_target_snapshot(account)) == expected
    assert calls == [("execution_read", {"op": "last_target", "account_id": account.id})]


def test_calculate_target_volume_does_not_restore_forbidden_symbols_from_last_target() -> None:
    """不应从上一次目标快照中恢复被禁止交易的品种。"""
    logger = WarningLogger()
    fake_self = cast(AbstractExecutor, cast(object, _VolumeCalcStub(logger)))
    account_assets = UnifiedAccountAssets(
        available_cash=100.0,
        total_asset=100.0,
        market_value=0.0,
        positions=[],
    )
    market_data = {
        "MA609": UnifiedPriceData(
            symbol="MA609",
            last_price=1.0,
            bid_price=1.0,
            ask_price=1.0,
            bid_volume=1.0,
            ask_volume=1.0,
            volume=1.0,
            turnover=1.0,
            timestamp=1,
            update_time="2026-03-13T00:00:00",
        )
    }

    result = AbstractExecutor.calculate_target_volume_base(
        fake_self,
        curr_target={"MA609": 0.1},
        account_assets=account_assets,
        market_data=market_data,
        trade_rules={},
        last_target={"rb2610": 0.2},
        forbidden_symbols=["rb2610"],
    )

    assert result == {"MA609": 10.0}
    assert logger.messages == []


def test_trade_serializes_success_result_to_json_safe_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """将成功执行结果序列化为可安全写入 JSON 的载荷。"""
    account = build_account()
    captured: dict[str, object] = {}

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> SimpleNamespace:
        result = cast("dict[str, object]", cast(UnifiedStandardOutput, output).model_dump(mode="json"))
        captured["account"] = account
        captured["raw_input"] = raw_input
        captured["result"] = result
        captured["execution_id"] = execution_id
        return SimpleNamespace(id=101, is_success=1)

    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account, **kwargs: FakeExecutor())
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    _ = asyncio.run(rebalance_execution.trade(account, {"ag2612": 0.1}))

    result = captured["result"]
    assert isinstance(result, dict)
    assert result["memory"]["ts"] == "2026-03-11T21:20:13"
    assert set(result) == {
        "account_assets",
        "inputs",
        "memory",
        "status",
        "symbol_results",
        "channel_type",
        "error",
        # 柔和版保留展示结论字段，值由 status 派生。
        "outcome",
        "outcome_reason",
        "reason_code",
        "execution_time",
        "success",
        "extra",
    }
    assert result["outcome"] == "completed"
    assert result["symbol_results"]["ag2612"]["algorithm"] == "SINGLE-MAKER"
    assert captured["execution_id"] is None


@pytest.mark.parametrize("cleanup", [False, True])
def test_trade_builds_model_before_execute_and_persists_dict(
    monkeypatch: pytest.MonkeyPatch,
    cleanup: bool,
) -> None:
    """标准输入在进入执行器前应已转成模型，落库时仍写入字典快照。"""
    account = build_account(feishu_key="secret-feishu-key")
    fake_executor = FakeExecutor()
    captured: dict[str, object] = {}

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> SimpleNamespace:
        _ = account, output
        captured["raw_input"] = raw_input
        captured["execution_id"] = execution_id
        return SimpleNamespace(id=105, is_success=1)

    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account, **kwargs: fake_executor)
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    _ = asyncio.run(rebalance_execution.trade(account, {"ag2612": 0.1}, cleanup=cleanup))

    assert isinstance(fake_executor.execute_input, UnifiedStandardInput)
    assert fake_executor.cleanup is False
    assert fake_executor.execute_input.curr_target == {"ag2612": 0.1}
    assert fake_executor.execute_input.feishu_key == "secret-feishu-key"

    raw_input = captured["raw_input"]
    assert isinstance(raw_input, dict)
    assert raw_input["curr_target"] == {"ag2612": 0.1}
    assert raw_input["feishu_key"] == "secret-feishu-key"
    assert "account_config" in raw_input


def test_trade_defaults_gm_ashare_short_leverage_to_zero_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GM+A股 且未显式配置 short_leverage 时，应默认按 long-only 处理负目标。"""
    account = build_account(
        name="gm-ashare-account",
        market="A股",
        trade_channel=TradeChannel.GM,
        brokerage="gm",
        account_config={
            "account_id": "gm-account",
            "token": "token",
            "connection_mode": "service",
            "serv_addr": "127.0.0.1:7001",
        },
        long_leverage=None,
        short_leverage=None,
        weight_precision=0.01,
    )
    manager = FakeWorkerBackendManager()

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> SimpleNamespace:
        _ = account, raw_input, output, execution_id
        return SimpleNamespace(id=108, is_success=1)

    monkeypatch.setattr(execution_backend, "get_worker_backend_manager", lambda: manager)
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    _ = asyncio.run(
        rebalance_execution.trade(
            account,
            {"SHSE.600000": 0.13, "SHSE.588000": -0.09},
        )
    )

    assert len(manager.trade_calls) == 1
    standard_input = cast("dict[str, object]", manager.trade_calls[0]["standard_input_dict"])
    assert standard_input["curr_target"] == {
        "600000.SH": 0.13,
        "588000.SH": 0.0,
    }


def test_trade_routes_gm_account_to_worker_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GM 调仓应走 worker manager，而不是主进程内创建执行器并写审计。"""
    account = build_account(
        name="gm-live-account",
        market="A股",
        trade_channel=TradeChannel.GM,
        brokerage="gm",
        account_config={
            "account_id": "gm-account",
            "token": "token",
            "connection_mode": "service",
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
    ) -> SimpleNamespace:
        _ = account, output
        captured["raw_input"] = raw_input
        captured["execution_id"] = execution_id
        return SimpleNamespace(id=205, is_success=1)

    def unexpected_create_executor_instance(_account: Account) -> FakeExecutor:
        raise AssertionError("GM 路径不应在主进程创建执行器")

    async def unexpected_append_execution_event(**_kwargs: object) -> bool:
        raise AssertionError("GM worker 路径不应在主进程写 execution event")

    async def unexpected_append_execution_artifact(**_kwargs: object) -> bool:
        raise AssertionError("GM worker 路径不应在主进程写 execution artifact")

    monkeypatch.setattr(worker_state, "create_executor_instance", unexpected_create_executor_instance)
    monkeypatch.setattr(execution_backend, "get_worker_backend_manager", lambda: manager)
    monkeypatch.setattr(execution_backend, "append_execution_event", unexpected_append_execution_event)
    monkeypatch.setattr(
        worker_audit, "append_execution_audit_batch_sync", lambda _batch: unexpected_append_execution_artifact()
    )
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    _ = asyncio.run(
        rebalance_execution.trade(
            account,
            {"SHSE.600000": 0.12},
            execution_id="exec-gm-worker-trade-1",
            trigger_source="manual",
        )
    )

    assert len(manager.trade_calls) == 1
    trade_call = manager.trade_calls[0]
    assert cast("dict[str, object]", trade_call["standard_input_dict"])["curr_target"] == {"600000.SH": 0.12}
    assert cast("dict[str, object]", trade_call["audit_input"])["curr_target"] == {"600000.SH": 0.12}
    assert trade_call["execution_id"] == "exec-gm-worker-trade-1"
    assert trade_call["trigger_source"] == "manual"
    assert captured["raw_input"] == cast(dict[str, object], trade_call["standard_input_dict"])
    assert captured["execution_id"] == "exec-gm-worker-trade-1"


def test_trade_routes_ctp_account_to_worker_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后端选择应由策略驱动，而不是写死 GM 渠道。"""
    account = build_account()
    manager = FakeWorkerBackendManager()

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> SimpleNamespace:
        _ = account, raw_input, output, execution_id
        return SimpleNamespace(id=305, is_success=1)

    def unexpected_create_executor_instance(_account: Account) -> FakeExecutor:
        raise AssertionError("PROCESS 策略下不应走线程执行器")

    monkeypatch.setattr(execution_backend, "get_worker_backend_manager", lambda: manager)
    monkeypatch.setattr(worker_state, "create_executor_instance", unexpected_create_executor_instance)
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    _ = asyncio.run(
        rebalance_execution.trade(
            account,
            {"rb2610": 0.12},
            execution_id="exec-ctp-worker-policy-1",
            trigger_source="manual",
        )
    )

    assert len(manager.trade_calls) == 1
    assert cast(Account, manager.trade_calls[0]["account"]).trade_channel == TradeChannel.CTP
    assert cast("dict[str, object]", manager.trade_calls[0]["standard_input_dict"])["channel_type"] == str(
        TradeChannel.CTP
    )


def test_trade_persists_failed_gm_worker_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GM worker 返回失败输出时，应落 error record 并保留结构化错误。"""
    account = build_account(
        name="gm-live-account",
        market="A股",
        trade_channel=TradeChannel.GM,
        brokerage="gm",
        account_config={
            "account_id": "gm-account",
            "token": "token",
            "connection_mode": "service",
            "serv_addr": "127.0.0.1:7001",
        },
    )
    manager = FakeWorkerBackendManager(
        trade_output=UnifiedStandardOutput(
            account_assets=UnifiedAccountAssets(
                available_cash=0.0,
                total_asset=0.0,
                market_value=0.0,
                positions=[],
            ),
            memory={"message": "boom"},
            inputs=None,
            execution_time=0.0,
            channel_type=TradeChannel.GM,
            symbol_results={},
            status=ExecutionStatus.FAILED,
            error="boom",
            success=False,
            extra={
                "worker_error": {
                    "type": "runtime_error",
                    "message": "boom",
                    "retryable": False,
                }
            },
        )
    )
    captured_error_record: dict[str, object] = {}

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: UnifiedStandardOutput,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> SimpleNamespace:
        _ = account
        raw_result = cast("dict[str, object]", output.model_dump(mode="json"))
        captured_error_record.update(
            {
                "account_id": 1,
                "raw_input": raw_input,
                "raw_result": raw_result,
                "msg": output.get_error_message(),
                "execution_id": execution_id,
            }
        )
        return SimpleNamespace(id=401, is_success=0)

    monkeypatch.setattr(execution_backend, "get_worker_backend_manager", lambda: manager)
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    record = asyncio.run(
        rebalance_execution.trade(
            account,
            {"SHSE.600000": 0.12},
            execution_id="exec-gm-worker-error-1",
            trigger_source="manual",
        )
    )

    assert record.id == 401
    assert record.is_success == 0
    assert captured_error_record["execution_id"] == "exec-gm-worker-error-1"
    assert captured_error_record["msg"] == "boom"
    assert captured_error_record["raw_result"]["extra"]["worker_error"] == {
        "type": "runtime_error",
        "message": "boom",
        "retryable": False,
    }


def test_account_rebalance_persists_same_normalized_target_used_by_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实调仓在下单前保存快照，且复用执行器收到的归一化目标对象。"""
    account = build_account(long_leverage=2.0, short_leverage=3.0)
    raw_target = {"rb2610": 0.5, "ag2612": -0.5}
    captured: dict[str, object] = {}

    async def fake_load(_account_id: int, _logger: object) -> Account:
        return account

    async def fake_check(_account: Account, execution_id: str | None = None) -> None:
        return None

    async def fake_resolve(_account: Account, _execution_id: str | None, _logger: object):
        return rebalance_execution._ResolvedRebalanceRequest(portfolio_id=7, curr_target=raw_target)

    async def fake_append(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(id=1)

    async def fake_trade(_account: Account, _target: dict[str, float], **kwargs: object) -> SimpleNamespace:
        captured["trade_normalized_target"] = kwargs["normalized_target"]
        return SimpleNamespace(id=10, is_success=1)

    monkeypatch.setattr(rebalance_execution, "_load_rebalance_account", fake_load)
    monkeypatch.setattr(rebalance_execution, "trade_channel_check", fake_check)
    monkeypatch.setattr(rebalance_execution, "_resolve_rebalance_request", fake_resolve)
    monkeypatch.setattr(rebalance_execution, "append_target_weight_snapshot", fake_append)
    monkeypatch.setattr(rebalance_execution, "trade", fake_trade)

    result = asyncio.run(rebalance_execution._run_account_rebalance(1, execution_id="exec-target-1"))

    assert result.is_success == 1
    assert captured["raw_weights"] == raw_target
    assert captured["normalized_weights"] == {"rb2610": 1.0, "ag2612": -1.5}
    assert captured["normalized_weights"] is captured["trade_normalized_target"]
    assert captured["source"] == "execution"


def test_trade_passes_execution_id_to_persisted_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """应将 execution 标识透传到执行记录持久化逻辑。"""
    account = build_account()
    captured: dict[str, object] = {}

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> SimpleNamespace:
        _ = account, raw_input, output
        captured["execution_id"] = execution_id
        return SimpleNamespace(id=105, is_success=1)

    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account, **kwargs: FakeExecutor())
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)
    monkeypatch.setattr(
        worker_audit, "append_execution_event_sync", lambda **kw: asyncio.run(noop_append_execution_event(**kw))
    )
    monkeypatch.setattr(worker_audit, "append_execution_audit_batch_sync", capture_worker_audit_batch())

    _ = asyncio.run(
        rebalance_execution.trade(
            account,
            {"ag2612": 0.1},
            execution_id="exec-test-1",
        )
    )

    assert captured["execution_id"] == "exec-test-1"


def test_trade_emits_execution_events_and_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    _worker_commands: dict[str, dict[str, object]],
) -> None:
    """成功执行交易时，应写入 execution 事件与附件。"""
    account = build_account(trade_rules={"ag2612": {"min_notional": 5.0}})
    captured_events: list[tuple[str, str]] = []
    captured_artifacts: list[str] = []
    artifact_versions: dict[str, int] = {}
    execution_summary_content: dict[str, object] | None = None
    target_snapshot_content: dict[str, object] | None = None

    async def fake_append_execution_event(**kwargs: object) -> bool:
        event_type = cast(ExecutionEventType, kwargs["event_type"])
        reason_code = cast(str, kwargs["reason_code"])
        captured_events.append((event_type.value, reason_code))
        return True

    async def fake_append_execution_artifact(**kwargs: object) -> bool:
        nonlocal execution_summary_content, target_snapshot_content
        artifact_type = cast(ExecutionArtifactType, kwargs["artifact_type"])
        captured_artifacts.append(artifact_type.value)
        artifact_versions[artifact_type.value] = cast(int, kwargs.get("schema_version", 1))
        if artifact_type == ExecutionArtifactType.EXECUTION_SUMMARY:
            execution_summary_content = cast("dict[str, object]", kwargs["content"])
        if artifact_type == ExecutionArtifactType.TARGET_SNAPSHOT:
            target_snapshot_content = cast("dict[str, object]", kwargs["content"])
        return True

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> SimpleNamespace:
        _ = account, raw_input, output, execution_id
        return SimpleNamespace(id=106, is_success=1)

    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account, **kwargs: FakeExecutor())
    monkeypatch.setattr(
        worker_audit, "append_execution_event_sync", lambda **kw: asyncio.run(fake_append_execution_event(**kw))
    )
    monkeypatch.setattr(
        worker_audit,
        "append_execution_audit_batch_sync",
        capture_worker_audit_batch(
            artifact=lambda **kw: asyncio.run(fake_append_execution_artifact(**kw)),
            event=lambda **kw: asyncio.run(fake_append_execution_event(**kw)),
        ),
    )
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    _ = asyncio.run(
        rebalance_execution.trade(
            account,
            {"ag2612": 0.1},
            execution_id="exec-audit-1",
            trigger_source="manual",
        )
    )

    assert captured_events[0][0] == "execution_started"
    assert ("input_snapshotted", "COMMON.INPUT_SNAPSHOTTED") in captured_events
    assert ("target_computed", "COMMON.TARGET_COMPUTED") in captured_events
    assert captured_events[-1] == ("execution_completed", "COMMON.EXECUTION_COMPLETED")
    assert "standard_input" in captured_artifacts
    assert "target_snapshot" in captured_artifacts
    assert "trade_rules_snapshot" in captured_artifacts
    assert "account_snapshot_before" in captured_artifacts
    assert "account_snapshot" in captured_artifacts
    assert "execution_summary" in captured_artifacts
    assert artifact_versions["target_snapshot"] == 3
    assert artifact_versions["execution_summary"] == 2
    assert execution_summary_content is not None
    assert execution_summary_content["summary"] == {
        "symbols_total": 1,
        "symbols_succeeded": 1,
        "symbols_failed": 0,
        "symbols_noop": 0,
    }
    assert execution_summary_content["success"] is True
    assert execution_summary_content["execution_time"] == 0.1
    reconciliation = cast("dict[str, object]", execution_summary_content["reconciliation"])
    account_block = cast("dict[str, object]", reconciliation["account"])
    assert account_block["source_before"] == "real"
    assert account_block["equity_before"] == 1000.0
    symbols = cast("list[dict[str, object]]", reconciliation["symbols"])
    ag_row = next(row for row in symbols if row["symbol"] == "ag2612")
    assert ag_row["target"] == 0.1
    assert ag_row["after"] == 0.0
    assert ag_row["reached"] is False
    assert target_snapshot_content is not None
    assert target_snapshot_content == {
        "business_facts_ref": {"version": 1, "view": "targets", "input_id": "input-test"}
    }
    frozen_input = _worker_commands["exec-audit-1"]
    assert frozen_input["strategy_target"] == {"ag2612": 0.1}
    assert frozen_input["curr_target"] == {"ag2612": 0.1}
    assert "account_target" not in frozen_input
    assert frozen_input["sizing_context"] == {
        "long_leverage": 1.0,
        "short_leverage": 1.0,
        "weight_precision": 0.001,
    }


def test_trade_prepares_execution_runtime_before_pre_execute_audit_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务端应在 pre-execute 审计事件前准备 runtime，并与 execute 内部继续共用同一序列。"""
    account = build_account()
    fake_executor: object | None = None
    captured_event_seqs: list[int] = []

    class _RuntimeAwareExecutor(FakeExecutor):
        prepared = False
        prepare_calls = 0
        execute_seq: int | None = None

        def prepare_execution_runtime(self) -> object:
            self.prepared = True
            self.prepare_calls += 1
            return object()

        def next_audit_seq(self) -> int:
            assert self.prepared is True
            return super().next_audit_seq()

        def execute(
            self,
            _standard_input: object,
            cleanup: bool = True,  # noqa: FBT002
            retain_runtime: bool = False,  # noqa: FBT001, FBT002
        ) -> UnifiedStandardOutput:
            _ = retain_runtime
            self.execute_seq = self.next_audit_seq()
            return super().execute(_standard_input, cleanup=cleanup)

    async def fake_append_execution_event(**kwargs: object) -> bool:
        captured_event_seqs.append(cast(int, kwargs["seq"]))
        return True

    async def fake_append_execution_artifact(**kwargs: object) -> bool:
        _ = kwargs
        return True

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: object,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> SimpleNamespace:
        _ = account, raw_input, output, execution_id
        return SimpleNamespace(id=107, is_success=1)

    def fake_create_executor_instance(_account: Account) -> _RuntimeAwareExecutor:
        nonlocal fake_executor
        fake_executor = _RuntimeAwareExecutor()
        return fake_executor

    monkeypatch.setattr(
        worker_state, "create_executor_instance", lambda account, **kwargs: fake_create_executor_instance(account)
    )
    monkeypatch.setattr(
        worker_audit, "append_execution_event_sync", lambda **kw: asyncio.run(fake_append_execution_event(**kw))
    )
    monkeypatch.setattr(
        worker_audit,
        "append_execution_audit_batch_sync",
        capture_worker_audit_batch(
            artifact=lambda **kw: asyncio.run(fake_append_execution_artifact(**kw)),
            event=lambda **kw: asyncio.run(fake_append_execution_event(**kw)),
        ),
    )
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    _ = asyncio.run(
        rebalance_execution.trade(
            account,
            {"ag2612": 0.1},
            execution_id="exec-runtime-seq-1",
            trigger_source="manual",
        )
    )

    assert fake_executor is not None
    assert fake_executor.prepare_calls == 1
    assert fake_executor.execute_seq == 4
    assert captured_event_seqs == [1, 2, 3, 5]


@pytest.mark.parametrize(
    ("output_status", "error_message", "expected_event_status", "expected_is_success"),
    [
        (ExecutionStatus.BLOCKED, "当前不在交易时间", ExecutionEventStatus.WARNING, 0),
        (ExecutionStatus.FAILED, "执行失败", ExecutionEventStatus.ERROR, 0),
    ],
)
def test_trade_completion_event_matches_output_status(
    monkeypatch: pytest.MonkeyPatch,
    output_status: ExecutionStatus,
    error_message: str,
    expected_event_status: ExecutionEventStatus,
    expected_is_success: int,
) -> None:
    """完成事件按输出分级；BLOCKED 与 FAILED 均未成功，但事件严重度不同。"""
    account = build_account()
    captured_events: list[dict[str, object]] = []
    captured_error_record: dict[str, object] = {}

    class _UnsuccessfulExecutor(FakeExecutor):
        def execute(
            self,
            _standard_input: object,
            cleanup: bool = True,  # noqa: FBT002
            retain_runtime: bool = False,  # noqa: FBT001, FBT002
        ) -> UnifiedStandardOutput:
            _ = retain_runtime
            self.execute_input = _standard_input
            self.cleanup = cleanup
            return UnifiedStandardOutput(
                account_assets=UnifiedAccountAssets(
                    available_cash=1000.0,
                    total_asset=1000.0,
                    market_value=0.0,
                    positions=[],
                ),
                memory={"message": error_message},
                inputs=None,
                execution_time=0.1,
                channel_type=TradeChannel.CTP,
                symbol_results={},
                status=output_status,
                error=error_message,
                success=False,
            )

    async def fake_append_execution_event(**kwargs: object) -> bool:
        captured_events.append(dict(kwargs))
        return True

    async def fake_append_execution_artifact(**_kwargs: object) -> bool:
        return True

    async def fake_append_output_record(
        *,
        account: object,
        raw_input: object,
        output: UnifiedStandardOutput,
        execution_id: str | None = None,
        execution_kind: object = None,
    ) -> SimpleNamespace:
        _ = account
        raw_result = cast("dict[str, object]", output.model_dump(mode="json"))
        captured_error_record.update(
            {
                "account_id": 1,
                "raw_input": raw_input,
                "raw_result": raw_result,
                "msg": output.get_error_message(),
                "execution_id": execution_id,
            }
        )
        return SimpleNamespace(id=301, is_success=expected_is_success)

    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account, **kwargs: _UnsuccessfulExecutor())
    monkeypatch.setattr(
        worker_audit, "append_execution_event_sync", lambda **kw: asyncio.run(fake_append_execution_event(**kw))
    )
    monkeypatch.setattr(
        worker_audit,
        "append_execution_audit_batch_sync",
        capture_worker_audit_batch(
            artifact=lambda **kw: asyncio.run(fake_append_execution_artifact(**kw)),
            event=lambda **kw: asyncio.run(fake_append_execution_event(**kw)),
        ),
    )
    monkeypatch.setattr(execution_backend, "_append_output_record", fake_append_output_record)

    record = asyncio.run(
        rebalance_execution.trade(
            account,
            {"ag2612": 0.1},
            execution_id="exec-unsuccessful-1",
            trigger_source="manual",
        )
    )

    assert record.id == 301
    assert record.is_success == expected_is_success
    assert captured_error_record["account_id"] == 1
    assert captured_error_record["execution_id"] == "exec-unsuccessful-1"
    assert captured_error_record["msg"] == error_message
    assert captured_error_record["raw_result"]["status"] == output_status.value
    assert captured_events[-1]["event_type"] == ExecutionEventType.EXECUTION_COMPLETED
    assert captured_events[-1]["status"] == expected_event_status
    assert captured_events[-1]["details"] == {
        "debug": {
            "success": False,
            "execution_status": output_status.value,
        },
        "order": {
            "orders_count": 0,
        },
    }


def test_trade_binds_account_control_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rebalance 入口应绑定账户控制防护层。"""
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
    ) -> SimpleNamespace:
        _ = account, raw_input, output, execution_id
        return SimpleNamespace(id=201, is_success=1)

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

    _ = asyncio.run(
        rebalance_execution.trade(
            account,
            {"ag2612": 0.1},
            execution_id="exec-bind-1",
        )
    )

    assert captured == {
        "account_id": 1,
        "channel": TradeChannel.CTP,
        "execution_id": "exec-bind-1",
    }
    assert fake_executor.executed_guard is fake_guard
    assert fake_executor.account_control_guard is None


def test_standard_input_accepts_database_trade_channel_string() -> None:
    """Text 列读回普通 str 时，调仓输入仍应能正常构造。"""
    account = build_account(
        account_config={
            "account_id": "gm-account",
            "token": "token",
            "connection_mode": "service",
            "serv_addr": "127.0.0.1:7001",
        }
    )
    account.trade_channel = cast("TradeChannel", "gm")

    standard_input = rebalance_execution._build_rebalance_standard_input(
        account=account,
        curr_target={"SHSE.600000": 0.1},
        last_target={"SZSE.000001": 0.2},
        execution_id="exec-db-channel-1",
        trigger_source="scheduler",
    )

    assert standard_input.channel_type == TradeChannel.GM
    assert standard_input.curr_target == {"600000.SH": 0.1}
    assert standard_input.last_target == {"000001.SZ": 0.2}
    assert cast("dict[str, object]", standard_input.extra["audit"])["channel"] == "gm"


def test_account_execution_timeout_flows_into_standard_input() -> None:
    """账户上配置的执行总超时必须进入标准输入，并经跨进程序列化往返保留。"""
    account = build_account(execution_timeout=45)

    standard_input = rebalance_execution._build_rebalance_standard_input(
        account=account,
        curr_target={"ag2612": 0.1},
        last_target={},
        execution_id="exec-timeout-1",
        trigger_source="scheduler",
    )
    assert standard_input.execution_timeout == 45

    # worker 后端在独立进程用 from_dict 重建输入，deadline 必须一起过去。
    restored = UnifiedStandardInput.from_dict(standard_input.to_dict())
    assert restored.execution_timeout == 45


def test_standard_input_execution_timeout_defaults_to_180() -> None:
    """未显式提供时，标准输入的总超时应回落到 180 秒的默认额度。"""
    payload: dict[str, object] = {
        "channel_type": TradeChannel.CTP.value,
        "account_config": {
            "broker_id": "9999",
            "investor_id": "test",
            "password": "test",
            "td_front": "tcp://td:1",
            "md_front": "tcp://md:2",
            "app_id": "app",
            "auth_code": "auth",
        },
        "curr_target": {},
        "last_target": {},
    }

    assert UnifiedStandardInput.from_dict(dict(payload)).execution_timeout == 180
    # 0 表示显式关闭 deadline（仿真路径依赖该语义），不能被默认值覆盖。
    assert UnifiedStandardInput.from_dict({**payload, "execution_timeout": 0}).execution_timeout == 0


@pytest.mark.parametrize("mode", ["success", "script_error", "cancelled"])
def test_portfolio_exception_contract_preserves_rebalance_audit(monkeypatch, mode):
    from unittest.mock import AsyncMock

    from axile.server.portfolio_function import PortfolioFunctionError

    account = build_account(id=2)
    error = PortfolioFunctionError(
        "remote original",
        error_line=12,
        error_type="ValueError",
        formatted_traceback="remote traceback",
        error_scope="account_connection",
    )
    calculate = AsyncMock(return_value={"x": 0.25})
    if mode != "success":
        calculate.side_effect = asyncio.CancelledError() if mode == "cancelled" else error
    record = AsyncMock()
    monkeypatch.setattr(rebalance_execution, "calculate_portfolio_for_account", calculate)
    monkeypatch.setattr(rebalance_execution, "append_error_execute_record", record)
    call = rebalance_execution._execute_portfolio_function(
        account, SimpleNamespace(custom_calc_py_code="code"), "e", None
    )
    if mode == "success":
        assert asyncio.run(call) == {"x": 0.25}
        record.assert_not_awaited()
    elif mode == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(call)
        record.assert_not_awaited()
    else:
        with pytest.raises(ValueError, match="第 12 行") as caught:
            asyncio.run(call)
        assert caught.value.__cause__ is error
        fields = record.await_args.kwargs
        assert fields["account_id"] == 2 and fields["execution_id"] == "e" and fields["error"] is error
        assert fields["raw_result"]["technical_detail"] == "remote original"
        assert fields["raw_result"]["traceback"] == "remote traceback"
        assert fields["raw_result"]["error_scope"] == "account_connection"
    calculate.assert_awaited_once_with(account, "code", execution_id="e")
