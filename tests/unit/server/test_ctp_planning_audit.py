"""CTP 调仓通过同一次账户观测规划并冻结执行前证据。"""

from types import SimpleNamespace

import pytest

from axile.common.serialization import to_payload
from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionArtifactType, ExecutionEventType
from axile.executor.ctp.ctp_execute import CTPExecutor, CtpSessionRecoveryRequired
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.termination import ExecutionTerminated
from axile.server.execution.worker_backend import worker, worker_audit
from axile.server.execution.worker_backend.protocol import WorkerBackendRequest
from tests.unit.server._execution_test_support import build_account, capture_worker_audit_batch


@pytest.mark.parametrize(
    "case", ["dispatch", "fact_write_failed", "audit_batch_failed", "closed", "terminated", "recovery"]
)
def test_ctp_worker_freezes_planning_assets_before_dispatch(monkeypatch, case):
    account = to_payload(build_account(), mode="json")
    executor = CTPExecutor(TradeChannel("ctp"))
    calls = []
    frozen = []
    events = []
    artifacts = []
    assets = UnifiedAccountAssets(
        available_cash=1000,
        total_asset=1100,
        market_value=100,
        positions=[
            Position(symbol="rb2610", direction=PositionDirection.LONG, volume=1, available_volume=1, market_value=100)
        ],
    )

    def prepare_catalog(_standard_input, *, include_account_symbols):
        assert not include_account_symbols
        calls.append("catalog")
        events.append({"event_type": "catalog", "seq": executor.next_audit_seq()})

    def read_assets(*, extend_catalog=None):
        assert extend_catalog == (None if case == "closed" else "allow_fill")
        calls.append("assets")
        if case == "recovery":
            raise CtpSessionRecoveryRequired("connection lost", return_code=-2)
        return assets

    def checkpoint():
        if case == "terminated":
            raise ExecutionTerminated(reason="operator stop", mode="graceful")

    def persist_input(**kwargs):
        calls.append("freeze")
        if case == "fact_write_failed":
            raise OSError("写入执行输入失败")
        frozen.append(kwargs["before_account_assets"])
        return "input-id"

    engine = executor._execution_engine()

    def after_planning(planning):
        assert planning.account_assets is assets
        assert len(frozen) == 1
        assert any(row["artifact_type"] == ExecutionArtifactType.ACCOUNT_SNAPSHOT_BEFORE for row in artifacts)
        calls.append("dispatch")
        engine._account_snapshot = assets, None
        return SimpleNamespace(phase_one_tasks=[], phase_two_plans=[], direct_results=[])

    monkeypatch.setattr(executor, "_verify_connection", lambda: True)
    monkeypatch.setattr(executor, "_prepare_instrument_catalog", prepare_catalog)
    monkeypatch.setattr(executor, "_check_trading_time", lambda: case != "closed")
    monkeypatch.setattr(executor, "_get_account_assets", read_assets)
    monkeypatch.setattr(executor, "handle_termination_checkpoint", checkpoint)
    monkeypatch.setattr(executor, "_execution_engine", lambda: engine)
    monkeypatch.setattr(engine, "_build_dispatch_phases", after_planning)
    monkeypatch.setattr(worker, "_resolve_prepared_executor", lambda **kwargs: executor)
    monkeypatch.setattr(worker, "_append_success_audit", lambda **kwargs: None)
    monkeypatch.setattr(worker, "_append_failed_audit", lambda **kwargs: None)
    monkeypatch.setattr(worker, "_finalize_with_orders", lambda *args: None)
    monkeypatch.setattr(worker_audit, "persist_pre_execute_input_sync", persist_input)
    monkeypatch.setattr(worker_audit, "execution_environment", lambda *args: {"version": "offline"})
    monkeypatch.setattr(
        worker_audit,
        "append_execution_audit_batch_sync",
        (
            (lambda _batch: 0)
            if case == "audit_batch_failed"
            else capture_worker_audit_batch(
                artifact=lambda **row: artifacts.append(row), event=lambda **row: events.append(row)
            )
        ),
    )

    response = worker._handle_worker_request(
        WorkerBackendRequest(
            request_id="ctp-trade",
            command="execute_trade",
            account_payload=account,
            execution_id="ctp-trade",
            payload={
                "standard_input": {"account_config": account["account_config"], "curr_target": {}, "last_target": {}},
                "trigger_source": "manual",
                "audit_input": {},
            },
        ),
        worker._WorkerBackendState(),
    )

    expected = {
        "dispatch": ["catalog", "assets", "freeze", "dispatch"],
        "terminated": ["catalog", "freeze"],
    }
    assert calls == expected.get(case, ["catalog", "assets", "freeze"])
    if case in {"dispatch", "closed"}:
        assert response.kind == "result", response.error
        assert frozen == [to_payload(assets, mode="json")]
        if case == "closed":
            assert response.output_payload["reason_code"] == "COMMON.SESSION.CLOSED"
    elif case == "terminated":
        assert response.kind == "terminated"
        assert frozen == [None]
    elif case == "recovery":
        assert response.kind == "error" and response.error is not None
        assert response.error.type == "ctp_session_recovery_required"
        assert frozen == [None]
    else:
        assert response.kind == "error", response
        return
    started = next(row for row in events if row["event_type"] == ExecutionEventType.EXECUTION_STARTED)
    assert started["seq"] < events[0]["seq"]
