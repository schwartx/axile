"""用保留的产品读取入口验证事实；附件夹具只写任务副本，不改源库。"""

import json
import tempfile
from contextlib import closing
from pathlib import Path

from axile.server._native import _core
from tests.sqlite_oracle import connect as oracle_connect
from tests.sqlite_oracle import sqlite_oracle


@sqlite_oracle
def _artifact_fixture(source, destination, artifact, account_id):
    with closing(oracle_connect(source)) as original, closing(oracle_connect(destination)) as copy:
        original.backup(copy)
        identity = artifact["execution_id"] or "fixture-input-owner"
        if artifact["execution_id"] is None:
            reference = artifact["content"]["business_facts_ref"]
            copy.execute(
                "UPDATE execution_input SET owner_execution_id=? WHERE id=?", (identity, reference["input_id"])
            )
        artifact = dict(artifact, execution_id=identity)
        owner = copy.execute(
            "SELECT account_id FROM executerecord WHERE execution_id=? UNION SELECT account_id FROM execution_input WHERE coalesce(owner_execution_id,execution_id)=? LIMIT 1",
            (identity, identity),
        ).fetchone()
        owner = owner[0] if owner else account_id or 1
        copy.execute(
            "INSERT OR IGNORE INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) VALUES (?,'evidence-fixture','期货','ctp','','2026-10-20','2026-10-20')",
            (owner,),
        )
        copy.execute(
            "INSERT OR IGNORE INTO execution_intent(execution_id,account_id,kind,trigger_source,status,channel,algorithm,payload,created_at) VALUES (?,?,'rebalance','manual','SUCCEEDED','ctp','SINGLE-MAKER','{}','2026-10-20')",
            (identity, owner),
        )
        copy.execute("DELETE FROM execution_artifact WHERE execution_id IS ?", (artifact["execution_id"],))
        copy.execute(
            "INSERT INTO execution_artifact(execution_id,artifact_type,schema_version,content,created_at) VALUES (?,?,?,?,?)",
            (
                artifact["execution_id"],
                artifact["artifact_type"],
                artifact.get("schema_version", 3),
                json.dumps(artifact["content"], ensure_ascii=False),
                artifact.get("created_at", "2026-10-20"),
            ),
        )
        copy.commit()
        return identity


@sqlite_oracle
def _input_owner(path, input_id):
    with closing(oracle_connect(path)) as connection:
        row = connection.execute(
            "SELECT account_id,coalesce(owner_execution_id,execution_id) FROM execution_input WHERE id=?", (input_id,)
        ).fetchone()
        return tuple(row) if row is not None else None


def artifact_view(path, artifact, *, account_id=None):
    with tempfile.TemporaryDirectory(prefix="axile-artifact-view-") as temporary:
        target = Path(temporary) / "fixture.db"
        identity = _artifact_fixture(str(path), str(target), artifact, account_id)
        try:
            public = json.loads(
                _core.list_execution_artifacts_json(
                    str(target), json.dumps({"execution_id": identity, "skip": 0, "limit": 500})
                )
            )
            if artifact["artifact_type"].lower() != "execution_summary" and account_id is None:
                return public["data"][0]["content"]
            selection = (
                {"execution_id": identity}
                if account_id is None or "business_facts_ref" not in artifact["content"]
                else {"execution_ids": [identity], "account_id": account_id}
            )
            data = _core.collect_evidence(
                str(target),
                json.dumps(
                    {**selection, "limits": {"executions": 100, "rows": 1000000, "bytes": 100000000, "seconds": 5}}
                ),
            )
            return data["executions"][identity]["artifacts"][0]["content"]
        finally:
            _core.close_database(str(target))


def read_view(path, kind, request):
    if kind == "read_execution_input":
        owner = _input_owner(str(path), request["input_id"])
        identity = owner[1] if owner else "missing"
        return artifact_view(
            path,
            {
                "execution_id": identity,
                "artifact_type": "standard_input",
                "content": {"business_facts_ref": {"version": 1, "view": "input", "input_id": request["input_id"]}},
            },
        )["input"]
    if kind == "resolve_execution_artifact":
        return artifact_view(path, request["artifact"], account_id=request.get("account_id"))
    value = json.loads(
        _core.execution_record_detail(_core.Store(str(path)), request["account_id"], request["record_id"])
    )
    if kind == "read_execution_result":
        return value["result"]
    return {
        key: value[key] for key in ("id", "account_id", "execution_id", "created_at", "is_success", "input", "result")
    }


async def read_execution_input(input_id):
    from axile.server.core.db import database_path

    return read_view(database_path(), "read_execution_input", {"input_id": input_id})


async def read_execution_result(record):
    from axile.server.core.db import database_path

    identity = record if isinstance(record, dict) else vars(record)
    try:
        return read_view(
            database_path(),
            "read_execution_result",
            {"record_id": identity.get("record_id", identity.get("id")), "account_id": identity["account_id"]},
        )
    except _core.NativeError as error:
        if error.args[0] in {"BUSINESS_FACTS", "INVALID_DATA"}:
            raise ValueError(error.args[1]) from None
        raise


async def read_execution_detail(record):
    from axile.server.core.db import database_path

    identity = record if isinstance(record, dict) else vars(record)
    try:
        return read_view(
            database_path(),
            "read_execution_detail",
            {"record_id": identity.get("record_id", identity.get("id")), "account_id": identity["account_id"]},
        )
    except _core.NativeError as error:
        if error.args[0] in {"BUSINESS_FACTS", "INVALID_DATA"}:
            raise ValueError(error.args[1]) from None
        raise


@sqlite_oracle
def _connection_fixture(path, account, state):
    from axile.server.db.models import Account
    from tests.unit.server._execution_test_support import insert_account

    with closing(oracle_connect(path)) as connection, connection:
        insert_account(connection, Account.model_validate(account))
        if state is not None:
            names = {row[1] for row in connection.execute("PRAGMA table_info(account_connection_state)")}
            values = {key: value for key, value in state.items() if key in names}
            for key in ("error", "presentation", "last_failure"):
                if values.get(key) is not None:
                    values[key] = json.dumps(values[key], ensure_ascii=False)
            keys = list(values)
            connection.execute(
                "INSERT INTO account_connection_state ("
                + ",".join(keys)
                + ") VALUES ("
                + ",".join("?" for _ in keys)
                + ")",
                [values[key] for key in keys],
            )


def connection_view(account, *, detail=False):
    from axile.server.connection_runtime import public_session_views
    from axile.server.db.models.account_connection import AccountConnectionDetail, AccountConnectionPublic

    with tempfile.TemporaryDirectory(prefix="axile-connection-view-") as temporary:
        path = str(Path(temporary) / "fixture.db")
        _core.migration_upgrade(path, "{}")
        state = account.connection_state.model_dump(mode="json") if account.connection_state else None
        _connection_fixture(path, account.model_dump(mode="json"), state)
        try:
            value = _core.account_connection_json(
                path,
                json.dumps({"account_id": account.id, "live_sessions": public_session_views()}, ensure_ascii=False),
            )
            return (AccountConnectionDetail if detail else AccountConnectionPublic).model_validate_json(value)
        finally:
            _core.close_database(path)


@sqlite_oracle
def _status_fixture(path, values, symbols, intent):
    with closing(oracle_connect(path)) as connection, connection:
        names = {row[1] for row in connection.execute("PRAGMA table_info(executerecord)")}
        values = {key: value for key, value in values.items() if key in names}
        for key, value in values.items():
            if isinstance(value, (dict, list)):
                values[key] = json.dumps(value, ensure_ascii=False)
        values["input_id"], values["result_input_id"] = None, None
        connection.execute(
            "INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) VALUES (?,'status-fixture','期货','ctp','','2026-10-20','2026-10-20')",
            (values["account_id"],),
        )
        keys = list(values)
        connection.execute(
            "INSERT INTO executerecord (" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")",
            [values[key] for key in keys],
        )
        if intent is not None:
            connection.execute(
                "INSERT INTO execution_intent(execution_id,account_id,kind,trigger_source,status,channel,algorithm,payload,created_at,error) VALUES (?,?,'rebalance','manual',?,'ctp','SINGLE-MAKER','{}',?,?)",
                (
                    values["execution_id"],
                    values["account_id"],
                    intent["status"],
                    values["created_at"],
                    intent.get("error"),
                ),
            )
        for index, symbol in enumerate(symbols):
            connection.execute(
                "INSERT INTO execution_symbol_result(record_id,path,ordinal,map_key,node_kind,status,outcome,audit_evidence,sizing_recorded,tick_recorded,target_volume_state,final_volume_state) VALUES (?,?,?,?, 'object','PARTIAL','not_reached','{}',0,0,'absent','absent')",
                (values["id"], "/symbol_results/" + symbol, index, symbol),
            )


def status_view(record, *, outcome_symbols=(), intent=None):
    with tempfile.TemporaryDirectory(prefix="axile-status-view-") as temporary:
        path = str(Path(temporary) / "fixture.db")
        _core.migration_upgrade(path, "{}")
        _status_fixture(path, record.model_dump(mode="json"), outcome_symbols, intent)
        try:
            return _core.execution_read(path, json.dumps({"op": "status", "execution_id": record.execution_id}))
        finally:
            _core.close_database(path)


def account_view(account):
    from axile.server.api.routes.account_crud import _account_read_request
    from axile.server.db.models import AccountPublic

    values = account.model_dump(mode="json")
    for name in ("has_pending_supplement", "notification_state", "connection_state"):
        value = getattr(account, name)
        values[name] = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return AccountPublic.model_validate(
        _core.account_public(
            "", json.dumps({"account": values, "runtime_sync": None, **_account_read_request()}, ensure_ascii=False)
        )
    )


def persist_record_fixture(path, request):
    """真实写入后从独立 SQL oracle 取得造数行，不模拟旧原生出口。"""
    from tests.unit.server._business_facts_test_support import rows

    receipt = _core.persist_execution_state(str(path), json.dumps(request, ensure_ascii=False, allow_nan=True))
    return next(row for row in rows(path, "executerecord") if row["id"] == receipt["record_id"])


def execution_receipt(*, record_id=1, account_id=1, status="FAILED", is_success=0, **fields):
    """生命周期编排用例的独立输入回执，预期由各用例明确指定。"""
    return {
        "record_id": record_id,
        "account_id": account_id,
        "status": status,
        "is_success": is_success,
        "finished_at": None,
        "error": None,
        "output_status": None,
        "outcome": None,
        "outcome_reason": None,
        "outcome_symbols": [],
        **fields,
    }


def receipt_record(receipt):
    """由独立 SQL oracle 检查真实回执对应的持久化行。"""
    from axile.server.core.db import database_path
    from tests.unit.server._business_facts_test_support import rows

    return next(row for row in rows(database_path(), "executerecord") if row["id"] == receipt["record_id"])
