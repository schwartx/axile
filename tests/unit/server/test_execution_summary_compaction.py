"""终态摘要去重保留冻结指标，独立 worker journal 保留原生执行证据。"""

from types import SimpleNamespace

from axile.domain.execution import ExecutionArtifactType
from axile.executor.models.execution_result import ExecutionStatus
from axile.server.execution.worker_backend import worker_audit
from tests.unit.server._execution_test_support import build_account, capture_worker_audit_batch


def test_worker_journal_keeps_native_orders_until_terminal_facts_commit(monkeypatch):
    artifacts = []
    result = {
        "status": "SUCCEEDED",
        "success": True,
        "account_assets": {"total_asset": 10.0, "positions": []},
        "symbol_results": {
            "A": {
                "status": "SUCCEEDED",
                "target_volume": 1.0,
                "final_volume": 1.0,
                "orders": [{"order_id": "journal-order", "filled_volume": 1.0, "avg_price": 10.0}],
                "trades": [{"trade_id": "journal-trade", "order_id": "journal-order", "trade_volume": 1.0}],
            }
        },
    }
    monkeypatch.setattr(worker_audit, "persist_post_execute_assets_sync", lambda **_kwargs: ("input-1", "asset-1"))
    monkeypatch.setattr(
        worker_audit,
        "append_execution_audit_batch_sync",
        capture_worker_audit_batch(artifact=lambda **kwargs: artifacts.append(kwargs)),
    )
    monkeypatch.setattr(worker_audit, "append_execution_event_sync", lambda **_kwargs: None)
    worker_audit._append_success_audit(
        account=build_account(),
        execution_id="pending-journal",
        algorithm_name="SINGLE-MAKER",
        output=SimpleNamespace(status=ExecutionStatus.SUCCEEDED, success=True),
        result=result,
        executor=object(),
    )
    summary = next(
        item["content"] for item in artifacts if item["artifact_type"] == ExecutionArtifactType.EXECUTION_SUMMARY
    )
    assert summary["symbol_results"]["A"]["orders"] == result["symbol_results"]["A"]["orders"]
    assert summary["symbol_results"]["A"]["trades"] == result["symbol_results"]["A"]["trades"]
    assert summary["reconciliation"]["symbols"][0]["orders"][0]["order_id"] == "journal-order"
