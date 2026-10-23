"""原生执行详情与迁移前完整 HTTP 契约保持一致。"""

import asyncio
import json
import pickle
import subprocess
import sys
from copy import deepcopy
from inspect import signature
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from axile.server._native import _core
from axile.server.api.routes import account_crud, account_schedule
from axile.server.business_facts.repository import read_execution_detail
from axile.server.db.models.supplement import SupplementGroup, SupplementStep
from axile.server.execution.legacy_compat import normalize_legacy_result
from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import persist_result
from tests.unit.server._execution_read_oracle import ExecuteRecordDetailPublic
from tests.unit.server._execution_test_support import build_account, insert_account
from tests.unit.server._performance_costs_oracle import project_execution
from tests.unit.server.test_business_facts_tq import tq_historical_payloads
from tests.unit.server.test_notification_compaction_projections import _CREATED, _EXECUTION, _event, _result

# 预期取自同库的冻结 0039 原生查询，经旧 HTTP 展示函数处理后的输出。
_DISPLAY_EDGE_CASES = [
    ("null", None, {"outcome": None}),
    ("invalid", "invalid", {"outcome": None}),
    ("empty", {}, {"outcome": None}),
    ("noop", {"status": "NOOP", "memory": {"private": "secret"}}, {"status": "NOOP", "outcome": "completed"}),
    ("unknown", {"status": "FUTURE", "memory": {"private": "secret"}}, {"status": "FUTURE", "outcome": None}),
    ("unknown_outcome", {"outcome": "unknown"}, {"outcome": "unknown", "status": None}),
    (
        "explicit_null",
        {"status": None, "error": None, "outcome": None, "outcome_reason": None, "reason_code": None, "memory": None},
        {"outcome": None},
    ),
    (
        "memory_reason",
        {"status": "PARTIAL", "memory": {"message": "RuntimeError: secret", "symbol_decision_reason_code": "CLOSED"}},
        {"status": "PARTIAL", "outcome": "not_reached", "error": "执行不到位", "reason_code": "COMMON.SESSION.CLOSED"},
    ),
    (
        "null_reason",
        {
            "status": "BLOCKED",
            "error": "当前不在交易时间",
            "reason_code": None,
            "memory": {"symbol_decision_reason_code": "CLOSED"},
        },
        {
            "status": "BLOCKED",
            "error": "当前不在交易时间",
            "reason_code": "COMMON.SESSION.CLOSED",
            "outcome": "blocked",
        },
    ),
    (
        "unknown_reason",
        {"status": "BLOCKED", "error": "当前不在交易时间", "reason_code": "FUTURE.CODE"},
        {"status": "BLOCKED", "error": "当前不在交易时间", "reason_code": "FUTURE.CODE", "outcome": "blocked"},
    ),
]


_SQLITE_ORACLE = """
import pickle, sqlite3, sys
from contextlib import closing
statements = pickle.loads(sys.stdin.buffer.read())
with closing(sqlite3.connect(sys.argv[1], timeout=2)) as connection, connection:
    result = [connection.execute(statement, parameters).fetchall() for statement, parameters in statements]
sys.stdout.buffer.write(pickle.dumps(result))
"""


def _oracle(path, statements):
    # 两个 SQLite 内核不能在同一进程持有同一 WAL 文件；每批 SQL 在子进程内提交。
    result = subprocess.run(
        [sys.executable, "-c", _SQLITE_ORACLE, str(path)],
        input=pickle.dumps(statements),
        capture_output=True,
        timeout=5,
        check=True,
    )
    return pickle.loads(result.stdout)


def _sql(path, statement, parameters=()):
    _oracle(path, [(statement, parameters)])


@pytest.fixture(autouse=True)
def _isolated_fixture_sql(monkeypatch):
    monkeypatch.setattr("tests.unit.server._business_facts_test_support.sql", _sql)


@pytest.mark.parametrize("page,journal", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize(
    "symbol,expected",
    [
        ({"error": "failed"}, {"error": "failed", "outcome": None}),
        ({"outcome": "error"}, {"outcome": "error", "status": "FAILED", "error": "执行失败，具体原因见执行证据"}),
        (
            {"status": "FAILED", "error": "RuntimeError: secret"},
            {"status": "FAILED", "outcome": "error", "error": "执行失败，具体原因见执行证据"},
        ),
        (
            {"status": "PARTIAL", "memory": {"message": "未达到目标"}},
            {"status": "PARTIAL", "outcome": "not_reached", "error": "未达到目标"},
        ),
        ({"outcome": "terminated"}, {"status": None, "outcome": "terminated", "error": "执行已终止"}),
        (
            {"status": "BLOCKED", "error": "当前不在交易时间", "reason_code": "CLOSED"},
            {
                "status": "BLOCKED",
                "error": "当前不在交易时间",
                "outcome": "blocked",
                "reason_code": "COMMON.SESSION.CLOSED",
            },
        ),
    ],
)
def test_execution_response_reuses_complete_legacy_display_contract(
    native_database, monkeypatch, page, journal, symbol, expected
):
    path = native_database
    _accounts(path)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
    monkeypatch.setattr(account_crud, "analysis_store", lambda: _core.Store(str(path)))
    monkeypatch.setattr(account_schedule, "analysis_store", lambda: _core.Store(str(path)))
    head = persist_result(
        path,
        {"symbol_results": {"A": symbol, **{name: source for name, source, _ in _DISPLAY_EDGE_CASES}}},
        raw_input={},
        created_at="2026-10-20T09:00:00",
    )
    name = "journal" if journal else "execution_records" if page else "execution_record_detail"
    operation = getattr(_core, name)
    native_bodies = []

    def capture(*args):
        body = operation(*args)
        native_bodies.append(body)
        return body

    monkeypatch.setattr(_core, name, capture)

    async def scenario():
        app = _app()
        app.include_router(account_schedule.router)
        route = "/1/activity" if journal else "/execute_records/1" if page else f"/execute_records/1/{head['id']}"
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.get(route)
        assert response.status_code == 200, response.text
        assert len(native_bodies) == 1
        assert response.content == native_bodies[0]
        decoded = response.json()
        if page:
            assert decoded["count"] == 1
            assert len(decoded["data"]) == 1
        actual = decoded["data"][0] if page else decoded
        if journal:
            actual = actual["record"]
        reference = {"A": expected, **{name: result for name, _, result in _DISPLAY_EDGE_CASES}}
        assert actual["symbol_results"] == reference, {
            name: (actual["symbol_results"].get(name), result)
            for name, result in reference.items()
            if actual["symbol_results"].get(name) != result
        }
        assert "secret" not in json.dumps(actual["symbol_results"], ensure_ascii=False)
        assert all("memory" not in result for result in actual["symbol_results"].values())

    asyncio.run(scenario())


def _expected_detail(head, detail):
    """公开 DTO 的默认值与旧品种展示投影独立于原生 HTTP 编码。"""
    normalized = normalize_legacy_result(detail["result"])
    symbols = normalized.get("symbol_results") if isinstance(normalized, dict) else None
    projected, _ = project_execution(SimpleNamespace(**detail))
    return ExecuteRecordDetailPublic.model_validate(
        {
            **head,
            **detail,
            "symbol_results": {
                symbol: {
                    key: item[key]
                    for key in ("status", "error", "outcome", "outcome_reason", "reason_code")
                    if key in item
                }
                if isinstance(item, dict)
                else {}
                for symbol, item in symbols.items()
            }
            if isinstance(symbols, dict)
            else None,
            "trade_count": projected["tradeCount"],
            "symbol_count": projected["symbolCount"],
        }
    ).model_dump(mode="json")


def _accounts(path):
    statements = []
    connection = SimpleNamespace(execute=lambda *args: statements.append(args))
    for identity in (1, 2):
        insert_account(connection, build_account(id=identity))
    _oracle(path, statements)


def _app():
    app = FastAPI()
    app.include_router(account_crud.router)
    route = next(
        route for route in account_crud.router.routes if route.path == "/execute_records/{account_id}/{record_id}"
    )
    assert route.dependant.dependencies == []
    assert set(signature(route.endpoint).parameters) == {"account_id", "record_id"}
    return app


def _historical_corruption(path, name, statement, parameters):
    """只暂停目标表的归属和引用守卫，注入历史损坏后立即恢复。"""
    guards = [
        (trigger, sql)
        for trigger, sql in _oracle(
            path, [("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (name,))]
        )[0]
        if trigger.endswith("_update")
        and ("_owner_" in trigger or trigger.endswith(("record_input_update", "notification_outbox_update")))
    ]
    assert guards
    statements = [(f'DROP TRIGGER "{trigger}"', ()) for trigger, _ in guards]
    statements.append((statement, parameters))
    statements.extend((sql, ()) for _, sql in guards)
    _oracle(path, statements)


@pytest.mark.parametrize(
    "source_input,source_result",
    [
        tq_historical_payloads(),
        (None, []),
        (False, "invalid"),
        ({}, {}),
        (
            {"channel_type": "ctp", "curr_target": {}, "last_target": None},
            {"status": "NOOP", "execution_time": 0.0, "symbol_results": {}},
        ),
        (
            {"curr_target": {"rb2610": 1}, "extra": {"zero": 0, "null": None}},
            {
                "status": "FAILED",
                "technical_detail": "错误\n" + "根因" * 300,
                "symbol_results": {
                    "rb2610": {
                        "status": "FAILED",
                        "error": None,
                        "sizing": {"reason_code": "SIZED", "target_quantity": 1},
                        "orders": [{"order_id": "o1", "volume": 1, "filled_volume": 1}],
                        "trades": [{"trade_id": "t1", "trade_volume": 1, "trade_price": 100}],
                    }
                },
            },
        ),
    ],
)
def test_native_detail_matches_repository_and_summary(native_database, monkeypatch, source_input, source_result):
    path = native_database
    _accounts(path)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
    monkeypatch.setattr(account_crud, "analysis_store", lambda: _core.Store(str(path)))
    head = persist_result(
        path,
        source_result,
        raw_input=source_input if isinstance(source_input, dict) else {},
        execution_id="canonical-detail",
        is_success=0,
    )
    record_id = head["id"]
    if not isinstance(source_input, dict):
        _sql(
            path,
            "UPDATE execution_input SET root_kind=?,root_value=? WHERE id=?",
            ("null" if source_input is None else "invalid", json.dumps(source_input), head["input_id"]),
        )

    async def scenario():
        expected = _expected_detail(head, await read_execution_detail(head))
        query = Mock(wraps=_core.execution_record_detail)
        monkeypatch.setattr(_core, "execution_record_detail", query)
        with patch.object(
            AsyncSession, "execute", side_effect=AssertionError("native detail must not execute Python SQL")
        ):
            assert json.loads((await account_crud.execute_record_detail(1, record_id)).body) == expected
            query.assert_called_once()
            assert query.call_args.args[1:] == (1, record_id)
            app = _app()
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                response = await client.get(f"/execute_records/1/{record_id}")
                assert response.status_code == 200, response.text
                assert response.json() == expected
                assert not {"raw_input", "raw_result"}.intersection(response.json())
                for account_id, requested_record in [(99, record_id), (1, 99999), (2, record_id)]:
                    response = await client.get(f"/execute_records/{account_id}/{requested_record}")
                    assert response.status_code == 404, response.text
        _historical_corruption(
            path,
            "executerecord",
            "UPDATE executerecord SET input_id='missing-input' WHERE id=?",
            (record_id,),
        )
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.get(f"/execute_records/1/{record_id}")
            assert response.status_code == 503, response.text

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "corruption",
    ["foreign_asset", "foreign_input", "foreign_result_input", "event_hash", "capture_hash", "missing_plan"],
)
def test_native_detail_frozen_context_ownership_and_hashes(native_database, monkeypatch, corruption):
    path = native_database
    _accounts(path)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
    monkeypatch.setattr(account_crud, "analysis_store", lambda: _core.Store(str(path)))
    execution_id = _EXECUTION
    created_at = _CREATED
    source_input, _ = tq_historical_payloads()
    source_result = _result()
    source_input.update(target_quantities={"A": 0, "B": None}, target_units={"A": 2})
    captured = deepcopy(source_result["extra"]["server_notification_context"])
    source_result["inputs"] = {**deepcopy(source_input), "curr_target": {"A": 0.75}}
    head = persist_result(path, source_result, raw_input=source_input, execution_id=execution_id, is_success=0)
    other = persist_result(
        path, source_result, raw_input=source_input, execution_id="foreign-frozen-owner", account_id=2, is_success=0
    )
    notification = _event()
    event_context = notification.context
    group = SupplementGroup(
        id="detail-frozen-group",
        account_id=1,
        fingerprint="detail-frozen-fingerprint",
        base_scheduled_at=created_at,
        expires_at="2026-10-20T23:00:00+08:00",
        configured_count=1,
        effective_count=1,
    )
    step = SupplementStep(
        group_id=group.id, index=1, scheduled_at=created_at, status="running", execution_id=execution_id
    )
    statements = []
    for name, item in (
        ("notification_event", notification),
        ("supplement_group", group),
        ("supplement_step", step),
    ):
        values = item.model_dump(mode="json")
        statements.append(
            (
                f"INSERT INTO {name} ({','.join('[' + key + ']' for key in values)}) VALUES ({','.join('?' for _ in values)})",
                [
                    json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                    for value in values.values()
                ],
            )
        )
    _oracle(path, statements)
    native_store_call(
        path,
        "intent_insert",
        {
            "account_id": 1,
            "execution_id": execution_id,
            "kind": "rebalance",
            "trigger_source": "manual",
            "channel": "ctp",
            "algorithm": "SINGLE-MAKER",
            "payload": {},
            "created_at": created_at,
            "now": created_at,
        },
    )
    native_store_call(
        path,
        "intent_cas",
        {
            "execution_id": execution_id,
            "expected": "QUEUED",
            "target": "RUNNING",
            "fields": {"started_at": created_at},
            "now": created_at,
        },
    )
    native_store_call(
        path,
        "intent_finish",
        {
            "execution_id": execution_id,
            "status": "PARTIAL",
            "finished_at": created_at,
            "now": created_at,
        },
    )
    record_id = head["id"]
    borrowed_rows, extra_rows = _oracle(
        path,
        [
            (
                "SELECT asset_id FROM business_asset_binding WHERE record_id=? AND path='/account_assets'",
                (other["id"],),
            ),
            ("SELECT extra FROM execution_extension WHERE record_id=?", (record_id,)),
        ],
    )
    borrowed_asset = borrowed_rows[0][0]
    extra = json.loads(extra_rows[0][0])
    assert borrowed_asset is not None
    assert extra["server_notification_context"]["$axile_notification_context"] == 1

    async def scenario():
        expected = _expected_detail(head, await read_execution_detail(head))
        assert expected["input"]["plan"]["quantities"] == {"A": 0, "B": None}
        assert expected["input"]["plan"]["units"] == {"A": 2}
        assert expected["result"]["extra"]["server_notification_context"] == captured
        app = _app()
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.get(f"/execute_records/1/{record_id}")
            assert response.status_code == 200, response.text
            assert response.json() == expected
            if corruption == "foreign_asset":
                _historical_corruption(
                    path,
                    "business_asset_binding",
                    "UPDATE business_asset_binding SET asset_id=? WHERE record_id=?",
                    (borrowed_asset, record_id),
                )
            elif corruption in {"foreign_input", "foreign_result_input"}:
                field = "input_id" if corruption == "foreign_input" else "result_input_id"
                _historical_corruption(
                    path,
                    "executerecord",
                    f"UPDATE executerecord SET {field}=? WHERE id=?",
                    (other["input_id"], record_id),
                )
            elif corruption == "event_hash":
                context = deepcopy(event_context)
                context["account"]["name"] = "tampered"
                _historical_corruption(
                    path,
                    "notification_event",
                    "UPDATE notification_event SET context=? WHERE id=?",
                    (json.dumps(context), notification.id),
                )
            elif corruption == "capture_hash":
                extra["server_notification_context"]["capture_sha256"] = "0" * 64
                _sql(path, "UPDATE execution_extension SET extra=? WHERE record_id=?", (json.dumps(extra), record_id))
            else:
                _sql(path, "DELETE FROM execution_input_plan WHERE input_id=?", (head["input_id"],))
            response = await client.get(f"/execute_records/1/{record_id}")
            assert response.status_code == 503, response.text

    asyncio.run(scenario())
