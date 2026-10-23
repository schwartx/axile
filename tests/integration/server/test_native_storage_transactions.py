"""原生业务阶段的真实 SQLite 回滚、发送顺序和重启恢复契约。"""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from axile.executor.order_submissions import OrderSubmissionFact, OrderSubmissionRecorder
from axile.server import order_submissions as submissions
from tests.conftest import native_store_call, require_native_store
from tests.unit.server._execution_test_support import build_account

_NOW = "2026-10-23T09:30:00+08:00"
_LATER = "2026-10-23T09:31:00+08:00"
_INPUT = {"curr_target": {"A": 2}, "channel_type": "ctp"}
_ASSETS = {
    "available_cash": 20.0,
    "total_asset": 100.0,
    "market_value": 80.0,
    "source": "synthetic",
    "update_time": _NOW,
    "positions": [{"symbol": "A", "volume": 2.0, "market_value": 80.0}],
}
_RESULT = {
    "status": "SUCCEEDED",
    "success": True,
    "inputs": _INPUT,
    "account_assets": _ASSETS,
    "symbol_results": {
        "A": {
            "symbol": "A",
            "status": "SUCCEEDED",
            "target_volume": 2,
            "first_tick": {"bid_price": 39.0, "ask_price": 40.0},
            "sizing": {"unit_multiplier": 1},
            "orders": [{"order_id": "o1", "direction": "BUY", "volume": 2, "price": 40.0}],
            "trades": [{"trade_id": "t1", "order_id": "o1", "trade_price": 40.0, "trade_volume": 2}],
            "memory": {"status": "done"},
        }
    },
    "extra": {
        "server_notification_context": {
            "execution": {"status": "SUCCEEDED", "notified_at": _NOW},
            "assets": {"total_assets": 100.0},
            "positions": [],
        }
    },
}


def _rows(path: Path, table: str) -> list[dict]:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        columns = ",".join(f'"{row[1]}"' for row in connection.execute(f'PRAGMA table_info("{table}")'))
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {columns}')]


def _snapshot(path: Path) -> dict[str, list[dict]]:
    with closing(sqlite3.connect(path)) as connection:
        tables = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
    return {name: _rows(path, name) for name in tables}


def _sql(path: Path, statement: str, parameters: tuple = ()) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(statement, parameters)


@pytest.fixture
def database(native_database: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _sql(
        native_database,
        "INSERT INTO portfolio (id,name,market,custom_calc_py_code,created_at,updated_at) VALUES (1,?,?,?,?,?)",
        ("transaction-test", "期货", "def calculate_portfolio(context):\n    return {}", _NOW, _NOW),
    )
    account = build_account(
        portfolio_id=1,
        cron_expr="*/15 * * * *",
        supplement={"count": 1, "interval_minutes": 1},
        created_at=_NOW,
        updated_at=_NOW,
    )
    receipt = native_store_call(
        native_database,
        "account_create",
        {"account": account.model_dump(mode="json"), "now": _NOW, "idempotency_key": None},
    )
    assert receipt["account"]["id"] == 1
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    return native_database


def _freeze(path: Path, execution_id: str) -> str:
    receipt = native_store_call(
        path,
        "freeze_execution_input",
        {
            "account_id": 1,
            "execution_id": execution_id,
            "input_id": f"input-{execution_id}",
            "payload": _INPUT,
            "before_assets": {"total_asset": 100.0, "positions": []},
            "created_at": _NOW,
        },
    )
    native_store_call(
        path, "freeze_execution_after_assets", {"account_id": 1, "execution_id": execution_id, "payload": _RESULT}
    )
    return receipt["input_id"]


def _terminal(path: Path, execution_id: str, **changes: object) -> dict:
    return native_store_call(
        path,
        "persist_execute_record",
        {
            "account_id": 1,
            "execution_id": execution_id,
            "created_at": _NOW,
            "is_success": 1,
            "input_id": f"candidate-{execution_id}",
            "result_input_id": f"result-{execution_id}",
            "raw_input": _INPUT,
            "raw_result": _RESULT,
            **changes,
        },
    )


def _group(path: Path, group_id: str, *, scheduled_at: str = _NOW) -> str:
    schedule = json.loads(_rows(path, "account_settings")[0]["schedule"])
    fingerprint = hashlib.sha256(
        json.dumps([schedule["cron_expr"], schedule["supplement"], "ctp", 1], sort_keys=True).encode()
    ).hexdigest()
    step_id = f"{group_id}-step"
    receipt = native_store_call(
        path,
        "supplement_create_group",
        {
            "account_id": 1,
            "group": {
                "id": group_id,
                "fingerprint": fingerprint,
                "base_scheduled_at": scheduled_at,
                "expires_at": "2026-10-23T09:45:00+08:00",
                "configured_count": 1,
                "effective_count": 0,
            },
            "steps": [{"id": step_id, "group_id": group_id, "index": 0, "scheduled_at": scheduled_at}],
        },
    )
    assert receipt is not None
    return step_id


def _intent(path: Path, execution_id: str, *, step_id: str | None = None, running: bool = False) -> None:
    native_store_call(
        path,
        "intent_insert",
        {
            "execution_id": execution_id,
            "account_id": 1,
            "kind": "rebalance",
            "trigger_source": "supplement" if step_id else "manual",
            "channel": "ctp",
            "algorithm": "SINGLE-MAKER",
            "payload": {"supplement_step_id": step_id} if step_id else {},
            "created_at": _NOW,
            "now": _NOW,
            "calendar_status_by_channel": {"ctp": "available_open"},
        },
    )
    if running:
        receipt = native_store_call(
            path,
            "intent_cas",
            {
                "execution_id": execution_id,
                "expected": "QUEUED",
                "target": "RUNNING",
                "fields": {"started_at": _NOW},
                "now": _NOW,
            },
        )
        assert receipt["changed"]


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("stage", ["asset_history", "summary_reference"])
def test_terminal_failure_rolls_back_every_domain(database: Path, frozen: bool, stage: str) -> None:
    core = require_native_store("persist_execute_record")
    execution_id = "terminal-rollback"
    if frozen:
        _freeze(database, execution_id)
    _sql(
        database,
        "INSERT INTO execution_artifact (execution_id,artifact_type,schema_version,content,created_at) "
        "VALUES (?,'EXECUTION_SUMMARY',2,?,?)",
        (execution_id, json.dumps({"symbol_results": _RESULT["symbol_results"]}), _NOW),
    )
    table, event = (
        ("account_asset_snapshot", "INSERT") if stage == "asset_history" else ("execution_artifact", "UPDATE")
    )
    _sql(database, f"CREATE TRIGGER fail_terminal BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT,'{stage}'); END")
    before = _snapshot(database)
    with pytest.raises(core.NativeError) as caught:
        _terminal(database, execution_id)
    assert caught.value.args[0] == "BUSINESS_FACTS_SQL"
    assert stage in caught.value.args[1]
    assert _snapshot(database) == before

    _sql(database, "DROP TRIGGER fail_terminal")
    record = _terminal(database, execution_id)
    assert record["facts_complete"]
    assert record["input_id"] == (f"input-{execution_id}" if frozen else f"candidate-{execution_id}")
    assert record["result_input_id"] == record["input_id"]
    for table in (
        "execution_input_target",
        "execution_symbol_result",
        "execution_order",
        "business_trade",
        "execution_extension",
        "business_asset_snapshot",
        "business_asset_position",
        "account_asset_snapshot",
    ):
        assert _rows(database, table), table
    summary = _rows(database, "execution_artifact")[0]
    assert summary["schema_version"] == 3
    assert json.loads(summary["content"])["business_facts_ref"]["record_id"] == record["id"]


def test_lost_worker_response_preserves_frozen_facts_and_first_terminal(database: Path) -> None:
    core = require_native_store("persist_execute_record")
    execution_id = "unknown-worker-response"
    input_id = _freeze(database, execution_id)
    frozen_assets = _rows(database, "business_asset_snapshot")
    frozen_positions = _rows(database, "business_asset_position")
    first = _terminal(
        database,
        execution_id,
        is_success=0,
        raw_input=None,
        raw_result={"status": "FAILED", "task_status": "FAILED", "error": "worker response lost"},
    )
    assert first["input_id"] == input_id
    assert first["is_success"] == 0
    assert first["facts_complete"]
    assert _rows(database, "business_asset_snapshot") == frozen_assets
    assert _rows(database, "business_asset_position") == frozen_positions
    assert not _rows(database, "business_asset_binding")
    assert not _rows(database, "account_asset_snapshot")
    committed = _snapshot(database)
    assert _terminal(database, execution_id)["id"] == first["id"]
    assert _snapshot(database) == committed
    with pytest.raises(core.NativeError) as caught:
        _terminal(database, execution_id, account_id=2)
    assert caught.value.args == ("BUSINESS_FACTS", "执行标识属于其他账户")
    assert _snapshot(database) == committed


def test_pending_submission_commit_failure_prevents_sdk_call(database: Path) -> None:
    core = require_native_store("save_order_submission")
    _sql(
        database,
        "CREATE TRIGGER fail_pending BEFORE INSERT ON order_submission "
        "BEGIN SELECT RAISE(ABORT,'pending-before-send'); END",
    )
    calls = []
    recorder = OrderSubmissionRecorder(1, lambda fact: submissions._save_fact(str(database), fact))

    def send() -> int:
        calls.append("sent")
        return 0

    with pytest.raises(core.NativeError) as caught:
        recorder.submit(send, symbol="A", order_id="o1", request_id=1)
    assert caught.value.args[0] == "DATABASE_QUERY"
    assert "pending-before-send" in caught.value.args[1]
    assert not calls
    assert not _rows(database, "order_submission")
    _sql(database, "DROP TRIGGER fail_pending")
    assert recorder.submit(send, symbol="A", order_id="o1", request_id=1) == 0
    assert calls == ["sent"]


def test_successful_sdk_send_survives_failed_outcome_commit(database: Path) -> None:
    core = require_native_store("save_order_submission")
    _sql(
        database,
        "CREATE TRIGGER fail_outcome BEFORE UPDATE ON order_submission "
        "BEGIN SELECT RAISE(ABORT,'outcome-after-send'); END",
    )
    failures = []

    def sink(fact: OrderSubmissionFact) -> None:
        try:
            submissions._save_fact(str(database), fact)
        except core.NativeError as error:
            failures.append(error)
            raise

    recorder = OrderSubmissionRecorder(1, sink)
    calls = []

    def send() -> int:
        pending = _rows(database, "order_submission")
        assert len(pending) == 1
        assert pending[0]["revision"] == 0
        assert json.loads(pending[0]["data"])["send_status"] == "pending"
        calls.append("sent")
        return 0

    assert recorder.submit(send, symbol="A", order_id="o1", request_id=1) == 0
    assert calls == ["sent"]
    assert len(failures) == 1
    assert failures[0].args[0] == "DATABASE_QUERY"
    assert "outcome-after-send" in failures[0].args[1]
    assert json.loads(_rows(database, "order_submission")[0]["data"])["send_status"] == "pending"

    _sql(database, "DROP TRIGGER fail_outcome")
    recorder.broker_result("o1", rejected=False)
    persisted = json.loads(_rows(database, "order_submission")[0]["data"])
    assert persisted["revision"] == 2
    assert persisted["send_status"] == "accepted"
    assert persisted["broker_status"] == "acknowledged"
    assert calls == ["sent"]


def test_callback_before_sdk_return_and_duplicate_commits_keep_newest_fact(database: Path) -> None:
    recorder = OrderSubmissionRecorder(1, lambda fact: submissions._save_fact(str(database), fact))

    def send() -> int:
        recorder.broker_result("o1", rejected=True, error_id=31)
        return 0

    assert recorder.submit(send, symbol="A", order_id="o1", request_id=1) == 0
    first = _rows(database, "order_submission")[0]
    fact = OrderSubmissionFact.model_validate_json(first["data"])
    assert fact.revision == 2
    assert fact.send_status == "accepted"
    assert fact.broker_status == "rejected"
    for revision in (2, 0, 1, 2):
        submissions._save_fact(
            str(database), fact.model_copy(update={"revision": revision, "broker_status": "acknowledged"})
        )
    recorder.broker_result("o1", rejected=False)
    assert _rows(database, "order_submission") == [first]


def test_portfolio_trial_failure_preserves_code_and_prior_snapshot(database: Path) -> None:
    core = require_native_store("portfolio_operation", "target_operation")
    prior = native_store_call(
        database,
        "target_operation",
        {
            "op": "append",
            "portfolio_id": 1,
            "account_id": 1,
            "raw_weights": {"A": 0.25},
            "normalized_weights": {"A": 0.2},
            "source": "execution",
            "execution_id": "durable-target",
            "calculated_at": _NOW,
        },
    )
    saved_portfolio = _rows(database, "portfolio")
    saved_heads = _rows(database, "target_weight_snapshot")
    saved_weights = _rows(database, "target_weight_snapshot_weight")
    assert saved_heads[0]["id"] == prior["id"]
    assert len(saved_weights) == 2
    _sql(
        database,
        "CREATE TRIGGER fail_trial_weight BEFORE INSERT ON target_weight_snapshot_weight "
        "WHEN NEW.map_key='reject' BEGIN SELECT RAISE(ABORT,'trial-weight-stage'); END",
    )
    request = {
        "op": "save_trial",
        "portfolio_id": 1,
        "account_id": 1,
        "changes": {
            "name": "new-name",
            "custom_calc_py_code": "def calculate_portfolio(context):\n    return {'A': 1}",
        },
        "updated_at": _LATER,
        "raw_weights": {"A": 0.5, "reject": 0.5},
        "normalized_weights": None,
        "source": "manual",
        "calculated_at": _LATER,
    }
    before = _snapshot(database)
    with pytest.raises(core.NativeError) as caught:
        native_store_call(database, "portfolio_operation", request)
    assert caught.value.args[0] == "TARGET_SQL"
    assert "trial-weight-stage" in caught.value.args[1]
    assert _snapshot(database) == before
    assert _rows(database, "portfolio") == saved_portfolio
    assert _rows(database, "target_weight_snapshot") == saved_heads
    assert _rows(database, "target_weight_snapshot_weight") == saved_weights

    _sql(database, "DROP TRIGGER fail_trial_weight")
    native_store_call(database, "portfolio_operation", request)
    assert _rows(database, "portfolio")[0]["custom_calc_py_code"] == request["changes"]["custom_calc_py_code"]
    heads = _rows(database, "target_weight_snapshot")
    assert len(heads) == 2 and heads[0] == saved_heads[0]
    weights = _rows(database, "target_weight_snapshot_weight")
    assert [row for row in weights if row["snapshot_id"] == prior["id"]] == saved_weights
    assert len(weights) == 4


@pytest.mark.parametrize("stage", ["outbox_insert", "context_reference"])
def test_intent_finish_supplement_outbox_and_reference_are_atomic(database: Path, stage: str) -> None:
    core = require_native_store("intent_finish")
    execution_id = "supplement-finish"
    _intent(database, execution_id, step_id=_group(database, "group-finish"), running=True)
    _terminal(database, execution_id)
    table, event = (
        ("notification_event", "INSERT") if stage == "outbox_insert" else ("execution_extension", "UPDATE OF extra")
    )
    _sql(database, f"CREATE TRIGGER fail_finish BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT,'{stage}'); END")
    before = _snapshot(database)
    request = {"execution_id": execution_id, "status": "SUCCEEDED", "finished_at": _LATER, "now": _LATER}
    with pytest.raises(core.NativeError) as caught:
        native_store_call(database, "intent_finish", request)
    assert caught.value.args[0]
    assert stage in caught.value.args[1]
    assert _snapshot(database) == before

    _sql(database, "DROP TRIGGER fail_finish")
    assert native_store_call(database, "intent_finish", request)["changed"]
    assert _rows(database, "execution_intent")[0]["status"] == "SUCCEEDED"
    assert _rows(database, "supplement_step")[0]["status"] == "finished"
    assert _rows(database, "supplement_group")[0]["status"] == "completed"
    event = _rows(database, "notification_event")
    assert len(event) == 1 and event[0]["id"] == f"execution:{execution_id}"
    extra = json.loads(_rows(database, "execution_extension")[0]["extra"])
    assert extra["server_notification_context"]["$axile_notification_context"] == 1
    committed = _snapshot(database)
    assert not native_store_call(database, "intent_finish", {**request, "status": "FAILED"})["changed"]
    assert _snapshot(database) == committed


def test_abandoned_notification_claim_recovers_once_without_resending(database: Path) -> None:
    for group_id, scheduled_at in (("first", _NOW), ("second", _LATER)):
        _group(database, group_id, scheduled_at=scheduled_at)
        assert native_store_call(
            database,
            "supplement_cancel_groups",
            {"account_id": 1, "group_id": group_id, "reason": "terminated", "now": _NOW},
        )
    first = native_store_call(database, "notifications_claim", {"account_id": 1})
    assert first["status"] == "calling"
    assert _rows(database, "notification_event")[0]["status"] == "calling"
    assert native_store_call(database, "notifications_claim", {"account_id": 1}) is None
    recovered = native_store_call(database, "notifications_recover", {"finished_at": _LATER})
    assert recovered["account_ids"] == [1]
    assert not recovered["summary_errors"]
    after_recovery = _snapshot(database)
    native_store_call(database, "notifications_recover", {"finished_at": "2026-10-23T09:32:00+08:00"})
    assert _snapshot(database) == after_recovery
    rows = _rows(database, "notification_event")
    assert rows[0]["status"] == "unknown"
    assert rows[1]["status"] == "pending"
    second = native_store_call(database, "notifications_claim", {"account_id": 1})
    assert second["sequence"] != first["sequence"]
    request = {"sequence": second["sequence"], "finished_at": _LATER, "ok": True, "record_summary": False}
    assert native_store_call(database, "notifications_finish", request)["updated"]
    committed = _snapshot(database)
    assert not native_store_call(database, "notifications_finish", {**request, "ok": False})["updated"]
    assert native_store_call(database, "notifications_claim", {"account_id": 1}) is None
    assert _snapshot(database) == committed


@pytest.mark.parametrize("interrupted_status", ["RUNNING", "FAILED"])
def test_failed_recovery_terminal_commit_remains_recoverable(
    database: Path, monkeypatch: pytest.MonkeyPatch, interrupted_status: str
) -> None:
    from axile.server.execution import dispatcher, notification_outbox
    from axile.server.execution.intents import PROCESS_INTERRUPTED_REASON
    from axile.server.execution.registry import clear_queued_execution, clear_running_execution

    core = require_native_store("intent_recovery", "persist_execute_record")
    execution_id = "interrupted-running"
    queued_id = "still-queued"
    _intent(database, execution_id, step_id=_group(database, "recover-group"), running=True)
    if interrupted_status == "FAILED":
        native_store_call(
            database,
            "intent_finish",
            {
                "execution_id": execution_id,
                "status": "FAILED",
                "error": PROCESS_INTERRUPTED_REASON,
                "finished_at": _NOW,
                "now": _NOW,
            },
        )
    _intent(database, "ordinary-failed")
    native_store_call(
        database,
        "intent_finish",
        {
            "execution_id": "ordinary-failed",
            "status": "FAILED",
            "error": "ordinary failure",
            "finished_at": _NOW,
            "now": _NOW,
        },
    )
    _intent(database, queued_id)
    monkeypatch.setattr(dispatcher, "wake_account_dispatcher", lambda _account_id: None)
    monkeypatch.setattr(notification_outbox, "wake_notifications", lambda _account_id: None)
    _sql(
        database,
        "CREATE TRIGGER fail_recovery BEFORE INSERT ON executerecord "
        "BEGIN SELECT RAISE(ABORT,'recovery-terminal'); END",
    )
    try:
        with pytest.raises(core.NativeError) as caught:
            asyncio.run(dispatcher.recover_intents_on_startup())
        assert caught.value.args[0] == "BUSINESS_FACTS_SQL"
        assert "recovery-terminal" in caught.value.args[1]
        assert not _rows(database, "executerecord")
        events = _rows(database, "notification_event")
        assert len(events) == 1
        statuses = {row["execution_id"]: row["status"] for row in _rows(database, "execution_intent")}
        assert statuses == {execution_id: "FAILED", queued_id: "QUEUED", "ordinary-failed": "FAILED"}
        _sql(database, "DROP TRIGGER fail_recovery")
        asyncio.run(dispatcher.recover_intents_on_startup())
        statuses = {row["execution_id"]: row["status"] for row in _rows(database, "execution_intent")}
        assert statuses == {execution_id: "FAILED", queued_id: "QUEUED", "ordinary-failed": "FAILED"}
        records = _rows(database, "executerecord")
        assert len(records) == 1 and records[0]["execution_id"] == execution_id
        assert _rows(database, "notification_event") == events
        committed = _snapshot(database)
        asyncio.run(dispatcher.recover_intents_on_startup())
        assert _snapshot(database) == committed
    finally:
        clear_queued_execution(1, queued_id)
        clear_running_execution(1, execution_id)
