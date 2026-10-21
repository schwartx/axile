"""终态摘要去重保留冻结指标，独立 worker journal 保留原生执行证据。"""

from copy import deepcopy
from types import SimpleNamespace

from axile.domain.execution import ExecutionArtifactType
from axile.executor.models.execution_result import ExecutionStatus
from axile.server.execution.execution_summaries import compact_execution_summary_content
from axile.server.execution.worker_backend import worker_audit
from tests.unit.server._execution_test_support import build_account, capture_worker_audit_batch


def test_compact_summary_keeps_outcomes_and_metrics_without_stable_subtrees():
    order = {"order_id": "order-1", "filled_volume": 0.0, "trades": [{"trade_id": "trade-1"}]}
    sizing = {"target_quantity": 0.0, "reason_code": "COMMON.SIZING.BELOW_MIN_QUANTITY"}
    original = {
        "status": "PARTIAL",
        "error": None,
        "reason_code": None,
        "outcome": "not_reached",
        "summary": {"symbols_total": 1, "symbols_failed": 1},
        "symbol_results": {
            "A": {
                "symbol": "A",
                "status": "PARTIAL",
                "error": None,
                "target_volume": 0.0,
                "final_volume": None,
                "success": False,
                "orders": [order],
                "trades": [{"trade_id": "trade-1"}],
                "first_tick": {"last_price": 10.0},
                "sizing": sizing,
                "memory": {"diagnostics": "synthetic"},
            }
        },
        "reconciliation": {
            "account": {"source_before": "unavailable", "equity_before": None},
            "symbols": [
                {
                    "symbol": "A",
                    "before": None,
                    "after": 0.0,
                    "reached": False,
                    "orders": [order],
                    "sizing": sizing,
                    "tca": {"n_orders": 1, "fee": 0.0},
                }
            ],
        },
    }
    before = deepcopy(original)
    compact = compact_execution_summary_content(original)
    assert compact["symbol_results"]["A"] == {
        "symbol": "A",
        "status": "PARTIAL",
        "error": None,
        "target_volume": 0.0,
        "final_volume": None,
        "success": False,
    }
    assert compact["reconciliation"]["symbols"] == [
        {"symbol": "A", "before": None, "after": 0.0, "reached": False, "tca": {"n_orders": 1, "fee": 0.0}}
    ]
    assert compact["summary"] == original["summary"]
    assert compact["reconciliation"]["account"] == original["reconciliation"]["account"]
    assert original == before


def test_compact_summary_preserves_absent_null_and_malformed_evidence():
    assert compact_execution_summary_content({"status": "FAILED"}) == {"status": "FAILED"}
    assert compact_execution_summary_content({"symbol_results": None, "reconciliation": None}) == {
        "symbol_results": None,
        "reconciliation": None,
    }
    malformed = {"symbol_results": {"A": None}, "reconciliation": {"symbols": [None, "legacy"]}}
    assert compact_execution_summary_content(malformed) == malformed


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
