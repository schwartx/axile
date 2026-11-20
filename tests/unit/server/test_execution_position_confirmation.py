"""持仓确认结果使用真实原生存储跨执行恢复，保留账户隔离。"""

import asyncio
from unittest.mock import Mock

import pytest

from axile.common.serialization import to_payload
from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind
from axile.executor.termination import ExecutionTerminated
from axile.server.execution.lifecycle import _record_terminated_execution, _resolve_termination_snapshot
from axile.server.execution.worker_backend import worker
from axile.server.execution.worker_backend.manager import WorkerBackendManager
from axile.server.execution.worker_backend.protocol import WorkerBackendRequest
from axile.server.execution.worker_backend.worker_state import _WorkerBackendState
from tests.conftest import native_store_call
from tests.unit.executor.test_execution_phase_settlement import SELL, assets, engine, inputs, result
from tests.unit.server._business_facts_test_support import read_result, seed_account
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_execution_derived_views import persist


def test_position_evidence_roundtrip_and_restart_guard(native_database):
    seed_account(native_database)
    seed_account(native_database, 2)
    row = {
        "symbol": "A",
        "status": "SUCCEEDED",
        "start_volume": 0,
        "expected_volume": 100,
        "position_confirmed": False,
        "final_volume": None,
    }

    def save(identity, symbols, account_id=1):
        return persist(
            native_database,
            account_id=account_id,
            execution_id=identity,
            input_id=identity + "-input",
            result_input_id=identity + "-result-input",
            raw_result={"status": "SUCCEEDED", "success": True, "symbol_results": symbols},
        )

    def pending(account_id=1):
        return native_store_call(
            native_database, "execution_read", {"op": "pending_positions", "account_id": account_id}
        )

    saved = save("pending", {"A": row})
    restored = read_result(native_database, {"id": saved["record_id"]})
    assert restored["symbol_results"]["A"]["start_volume"] == 0
    assert restored["symbol_results"]["A"]["expected_volume"] == 100
    assert restored["symbol_results"]["A"]["position_confirmed"] is False
    assert pending() == {"A": 100}
    assert pending(2) == {}
    save("other-account", {"B": {**row, "symbol": "B", "expected_volume": 200}}, 2)
    save("unrelated-failure", {})
    assert pending() == {"A": 100}
    save("still-pending", {"A": {**row, "status": "BLOCKED"}})
    assert pending() == {"A": 100}
    save("confirmed", {"A": {**row, "position_confirmed": True, "final_volume": 100}})
    assert pending() == {}
    assert pending(2) == {"B": 200}


def test_terminated_gm_rebalance_restores_pending_fill_in_replacement_worker(native_database, monkeypatch):
    seed_account(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    before = assets(sell=900)
    old = engine([before, before], channel="gm")
    old._runtime.sleep_or_terminate.side_effect = ExecutionTerminated(reason="停止", mode="graceful")
    monkeypatch.setattr(old, "_run_prepared_symbol_algorithms", Mock(return_value=[result(observed=900)]))
    monkeypatch.setattr(worker, "_resolve_prepared_executor", lambda **_: None)
    monkeypatch.setattr(worker, "_execute_trade_with_audit", lambda **_: old.run(inputs(old)))
    gm_config = {"connection_mode": "service", "account_id": "test", "token": "test", "serv_addr": "127.0.0.1:7001"}
    account = build_account(market="股票", trade_channel=TradeChannel("gm"), account_config=gm_config)
    command = WorkerBackendRequest(
        request_id="req",
        command="execute_trade",
        account_payload=to_payload(account),
        execution_id="gm-position-wait",
        payload={
            "standard_input": {"channel_type": "gm", "account_config": gm_config, "curr_target": {SELL: 0.2}},
            "trigger_source": "scheduled",
            "audit_input": {},
        },
    )
    response = worker._handle_execute_trade(command, _WorkerBackendState())
    request_for_pending = {"op": "pending_positions", "account_id": 1}
    assert native_store_call(native_database, "execution_read", request_for_pending) == {SELL: 500}

    with pytest.raises(ExecutionTerminated) as received:
        WorkerBackendManager._handle_response(response)

    record = asyncio.run(
        _record_terminated_execution(
            account_id=1,
            execution_id="gm-position-wait",
            execution_kind=ExecutionKind.REBALANCE,
            current_state=None,
            requested_at="2026-11-20T09:32:00",
            finished_at="2026-11-20T09:32:01",
            termination=_resolve_termination_snapshot(received.value, None),
        )
    )
    assert record["status"] == "TERMINATED"
    assert record["outcome"] == "terminated"

    replacement = engine([before], channel="gm")
    dispatch = Mock()
    monkeypatch.setattr(replacement, "_run_prepared_symbol_algorithms", dispatch)
    request = inputs(replacement)
    request.extra["pending_positions"] = native_store_call(native_database, "execution_read", request_for_pending)
    assert request.extra["pending_positions"] == {SELL: 500}
    assert replacement.run(request).status == "BLOCKED"
    dispatch.assert_not_called()
