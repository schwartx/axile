"""账户路由的配置校验与原生事务契约。"""

from __future__ import annotations

from contextlib import closing
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.registry import (
    ensure_default_account_control_registry_bootstrapped,
    reset_default_account_control_registry_for_tests,
)
from axile.server._native import _core
from axile.server.api.deps import get_scheduler
from axile.server.api.routes import account as account_routes
from axile.server.api.routes import account_crud as account_crud_routes
from axile.server.db.models import Account, AccountRuntimeSync
from tests.conftest import native_store_call
from tests.sqlite_oracle import connect as oracle_connect


@pytest.fixture(autouse=True)
def _seed_account_control_registry() -> None:
    registry = reset_default_account_control_registry_for_tests()
    ensure_default_account_control_registry_bootstrapped()
    registry.freeze()


class _Scheduler:
    def get_job(self, _job_id: str) -> None:
        return None


def _build_account(
    *,
    preset: str = "default",
    trade_channel: TradeChannel = TradeChannel.CTP,
    override: dict[str, object] | None = None,
    **changes: object,
) -> Account:
    data = {
        "id": 1,
        "name": "ctp-testnet-sim",
        "market": "期货",
        "trade_channel": trade_channel,
        "account_config": {
            "broker_id": "9999",
            "investor_id": "test",
            "password": "test",
            "td_front": "tcp://td:1",
            "md_front": "tcp://md:2",
            "app_id": "app",
            "auth_code": "auth",
        },
        "is_started": True,
        "cron_expr": "*/5 * * * *",
        "remark": None,
        "brokerage": "ctp",
        "weight_precision": 0.001,
        "long_leverage": 1.0,
        "short_leverage": 1.0,
        "algorithm": {"method": "SINGLE-MAKER", "params": {}},
        "empty_positions_algorithm": None,
        "trade_rules": {},
        "forbidden_symbols": [],
        "risk_symbols": [],
        "feishu_key": None,
        "portfolio_id": 1,
        "write_empty_record": 0,
        "account_control_preset": preset,
        "account_control_override": override,
    }
    return Account.model_validate(data | changes)


def _account_payload() -> dict[str, object]:
    return (
        _build_account()
        .snapshot()
        .model_dump(
            mode="json",
            exclude={"id", "created_at", "updated_at", "copied_from_account_id", "copied_from_account_name"},
        )
    )


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(account_routes.router)
    app.dependency_overrides[get_scheduler] = _Scheduler
    return app


async def _synchronized_runtime_sync(account_id: int, *_args: object, **_kwargs: object) -> AccountRuntimeSync:
    return AccountRuntimeSync(account_id=account_id, status="synchronized")


def _seed_account(database, **changes: object) -> Account:
    saved = native_store_call(
        database,
        "account_create",
        {"account": _build_account(**changes).model_dump(mode="json"), "now": "2026-10-23T09:00:00"},
    )
    return Account.model_validate(saved["account"])


def _load_account(database, account_id: int = 1) -> Account:
    return Account.model_validate(native_store_call(database, "account_get", {"account_id": account_id}))


@pytest.fixture
def account_client(native_database, monkeypatch):
    from axile.server.core import db
    from axile.server.execution import notification_outbox, supplements

    ensure_default_account_control_registry_bootstrapped()
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    monkeypatch.setattr(account_crud_routes, "reconcile_account_runtime", _synchronized_runtime_sync)
    monkeypatch.setattr(supplements, "reap_cancelled_queue", AsyncMock())
    monkeypatch.setattr(notification_outbox, "wake_notifications", lambda _id: None)
    monkeypatch.setattr(account_crud_routes, "get_running_execution_id", lambda _id: None)
    monkeypatch.setattr(account_crud_routes, "get_queued_execution_id", lambda _id: None)
    monkeypatch.setattr(account_crud_routes, "drop_account_worker", AsyncMock())
    with closing(oracle_connect(native_database)) as connection, connection:
        connection.execute(
            "INSERT INTO portfolio(id,name,market,custom_calc_py_code,created_at,updated_at) VALUES(1,?,?,?,?,?)",
            ("组合", "期货", "def calculate_portfolio(context): return {}", "2026-10-23", "2026-10-23"),
        )
    with TestClient(_build_app()) as client:
        yield client, native_database


def test_account_public_returns_connection_credentials_but_omits_webhook() -> None:
    account = _build_account(feishu_key="unique-feishu-webhook-key")
    account.account_config["password"] = "unique-account-password"
    payload = account_crud_routes._account_public(account).model_dump_json()
    assert "unique-account-password" in payload
    assert "unique-feishu-webhook-key" not in payload
    assert '"account_config"' not in payload and '"feishu_key"' not in payload
    assert '"account_configured":true' in payload and '"feishu_configured":true' in payload


def test_native_public_queries_match_existing_public_contract(account_client) -> None:
    client, database = account_client
    source = _seed_account(database, feishu_key="private-webhook")
    expected = account_crud_routes._account_public(source).model_dump(mode="json")
    detail = client.get("/account/1")
    listing = client.get("/account/")
    assert detail.status_code == 200, detail.text
    assert listing.status_code == 200, listing.text
    assert detail.json() == expected
    assert listing.json() == {"data": [expected]}
    assert "private-webhook" not in detail.text


@pytest.mark.parametrize(
    "mutation",
    [
        *[
            pytest.param(
                f"UPDATE account_settings SET {column}='[]' WHERE account_id=1",
                id=f"{column}-array",
            )
            for column in ("connection", "execution", "schedule", "notification", "backtest")
        ],
        pytest.param("UPDATE account_settings SET connection='{' WHERE account_id=1", id="malformed-json"),
        pytest.param("UPDATE account SET name=x'FF' WHERE id=1", id="binary-field"),
        pytest.param("UPDATE account SET name=CAST(x'FF' AS TEXT) WHERE id=1", id="invalid-utf8"),
    ],
)
def test_corrupt_account_data_is_a_server_error(account_client, mutation) -> None:
    client, database = account_client
    _seed_account(database)
    with closing(oracle_connect(database)) as connection, connection:
        connection.execute(mutation)

    with pytest.raises(_core.NativeError) as caught:
        native_store_call(database, "account_get", {"account_id": 1})
    assert caught.value.args[0] == "ACCOUNT_DATA"
    for path in ("/account/1", "/account/"):
        response = client.get(path)
        assert response.status_code == 500, response.text
        assert response.json()["detail"] == caught.value.args[1]


def test_invalid_account_request_keeps_input_error(account_client) -> None:
    client, database = account_client
    _seed_account(database)
    with pytest.raises(_core.NativeError) as caught:
        native_store_call(
            database,
            "account_update",
            {"account_id": 1, "patch": {"account_config": []}, "now": "2026-10-24T12:00:00"},
        )
    assert caught.value.args[0] == "ACCOUNT_INPUT"
    assert client.patch("/account/1", json={"account_config": []}).status_code == 422


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"account_control_preset": "unknown"}, "未知"),
        ({"account_config": {"td_front": "tcp://host"}}, "account_config.td_front:"),
        ({"account_config": {"legacy_option": True}}, "account_config.legacy_option:"),
    ],
)
def test_create_rejects_invalid_configuration_before_write(account_client, patch, message) -> None:
    client, database = account_client
    payload = _account_payload()
    if "account_config" in patch:
        payload["account_config"] |= patch["account_config"]
    else:
        payload |= patch
    response = client.post("/account/", json=payload)
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert native_store_call(database, "account_list", {}) == []


def test_create_account_requires_explicit_account_control_preset(account_client) -> None:
    client, _ = account_client
    payload = _account_payload()
    payload.pop("account_control_preset")
    assert client.post("/account/", json=payload).status_code == 422


def test_create_rejects_incompatible_preset(account_client) -> None:
    client, _ = account_client
    payload = _account_payload() | {
        "trade_channel": "gm",
        "account_control_preset": "ctp",
        "account_config": {"account_id": "gm-account", "token": "token", "serv_addr": "127.0.0.1:7001"},
    }
    response = client.post("/account/", json=payload)
    assert response.status_code == 422
    assert "不兼容" in response.json()["detail"]


@pytest.mark.parametrize("creating", [True, False])
def test_normalizes_channel_config_before_persistence(account_client, creating) -> None:
    client, database = account_client
    config = {
        "channel_type": "ctp",
        "account_mode": "sim",
        "tq_username": "user",
        "tq_password": "secret",
        "broker_name": "hidden-broker",
        "account_id": "hidden-account",
        "account_password": "hidden-password",
    }
    if creating:
        response = client.post("/account/", json=_account_payload() | {"trade_channel": "tq", "account_config": config})
    else:
        _seed_account(database)
        response = client.patch("/account/1", json={"trade_channel": "tq", "account_config": config})
    assert response.status_code == (201 if creating else 200), response.text
    assert "account_config" not in response.json()
    assert _load_account(database).account_config == {
        "account_mode": "sim",
        "tq_username": "user",
        "tq_password": "secret",
        "initial_balance": 10_000_000.0,
    }


def test_update_preset_override_replacement_and_null(account_client) -> None:
    client, database = account_client
    _seed_account(database, override={"timezone": "Asia/Shanghai"})
    response = client.patch(
        "/account/1",
        json={
            "account_control_preset": "ctp",
            "account_control_override": {
                "operations": {"cancel_order": {"account": {"per_day": {"limit": 8, "on_trigger": "block"}}}},
            },
        },
    )
    assert response.status_code == 200, response.text
    override = response.json()["account_control_override"]
    assert override["timezone"] is None
    assert set(override["operations"]) == {"cancel_order"}
    assert _load_account(database).account_control_override.operations["cancel_order"].account.per_day.limit == 8
    cleared = client.patch("/account/1", json={"account_control_override": None})
    assert cleared.status_code == 200
    assert _load_account(database).account_control_override is None


def test_update_rejects_removed_insert_order_override(account_client) -> None:
    client, database = account_client
    _seed_account(database)
    response = client.patch(
        "/account/1",
        json={
            "account_control_override": {
                "operations": {"insert_order": {"account": {"per_minute": {"limit": 3, "on_trigger": "wait"}}}},
            }
        },
    )
    assert response.status_code == 422
    assert "未注册的 operation key" in str(response.json()["detail"])


def test_update_risk_symbols_persists_and_clears(account_client) -> None:
    client, database = account_client
    _seed_account(database)
    assert client.patch("/account/1", json={"risk_symbols": ["rb2610"]}).status_code == 200
    assert _load_account(database).risk_symbols == ["rb2610"]
    assert client.patch("/account/1", json={"risk_symbols": None}).status_code == 200
    assert _load_account(database).risk_symbols is None


@pytest.mark.parametrize("weight_precision", [0, -0.01, 0.03])
def test_invalid_weight_precision_does_not_mutate(account_client, weight_precision) -> None:
    client, database = account_client
    _seed_account(database)
    assert client.patch("/account/1", json={"weight_precision": weight_precision}).status_code == 422
    assert _load_account(database).weight_precision == 0.001
    assert client.post("/account/", json=_account_payload() | {"weight_precision": weight_precision}).status_code == 422
    assert len(native_store_call(database, "account_list", {})) == 1


def test_update_same_channel_preserves_omitted_credentials(account_client) -> None:
    client, database = account_client
    _seed_account(database)
    response = client.patch("/account/1", json={"account_config": {"td_front": "tcp://new:1"}})
    assert response.status_code == 200
    assert _load_account(database).account_config["password"] == "test"
    assert _load_account(database).account_config["td_front"] == "tcp://new:1"


@pytest.mark.parametrize(
    ("patch", "reset_expected"),
    [
        ({"is_started": False}, False),
        ({"is_started": True}, False),
        ({"algorithm": {"method": "SINGLE-MAKER", "params": {"price_strategy": "PASSIVE"}}}, False),
        ({"trade_channel": "gm"}, False),
        ({"account_config": {"serv_addr": "127.0.0.1:7001"}}, False),
        ({"account_config": {"serv_addr": "127.0.0.1:7002"}}, True),
        ({"account_config": {"token": "new-token"}}, True),
    ],
)
def test_update_gm_resets_only_changed_connection(account_client, patch, reset_expected) -> None:
    client, database = account_client
    _seed_account(
        database,
        trade_channel=TradeChannel.GM,
        account_config={
            "connection_mode": "service",
            "account_id": "test-account",
            "token": "test-token",
            "serv_addr": "127.0.0.1:7001",
        },
    )
    response = client.patch("/account/1", json=patch)
    assert response.status_code == 200, response.text
    saved = native_store_call(database, "account_get", {"account_id": 1})
    assert saved["runtime_sync"]["reset_worker"] is reset_expected
    assert (saved["connection_state"] is not None) is reset_expected


@pytest.mark.parametrize("condition", ["started", "scheduled", "running", "queued"])
def test_copy_rejects_active_source(account_client, monkeypatch, condition) -> None:
    client, database = account_client
    _seed_account(database, is_started=condition == "started")
    if condition == "scheduled":
        monkeypatch.setattr(_Scheduler, "get_job", lambda *_args: object())
    elif condition == "running":
        monkeypatch.setattr(account_crud_routes, "get_running_execution_id", lambda _id: "running")
    elif condition == "queued":
        monkeypatch.setattr(account_crud_routes, "get_queued_execution_id", lambda _id: "queued")
    assert client.post("/account/1/copy", json={"name": "新账户"}).status_code == 409
    assert len(native_store_call(database, "account_list", {})) == 1


def test_copy_preserves_config_and_starts_without_source_history(account_client) -> None:
    client, database = account_client
    source = _seed_account(database, is_started=False, feishu_key="server-only-webhook")
    with closing(oracle_connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO executerecord(account_id,execution_id,is_success,created_at,facts_complete,"
            "audit_evidence,termination_evidence,result_input_kind,termination_kind,termination_cancel_ids_kind) "
            "VALUES(1,'old',1,'2026-10-22',0,'{}','{}','absent','absent','absent')"
        )
        connection.execute(
            "INSERT INTO account_notification_state(account_id,last_success_at,execution_id) VALUES(1,'old','old')"
        )
        connection.execute(
            "INSERT INTO portfolioaccount(account_id,portfolio_id,created_at) VALUES(1,NULL,'2026-10-22')"
        )
        connection.execute("INSERT INTO portfolioaccount(account_id,portfolio_id,created_at) VALUES(1,1,'2026-10-23')")
    response = client.post("/account/1/copy", json={"name": "  新账户  "})
    assert response.status_code == 201, response.text
    assert "server-only-webhook" not in response.text
    copied = _load_account(database, response.json()["id"])
    assert copied.name == "新账户" and copied.is_started is False
    assert copied.copied_from_account_id == source.id and copied.copied_from_account_name == source.name
    assert copied.account_config["password"] == source.account_config["password"]
    assert copied.feishu_key == source.feishu_key
    assert copied.notification_state is None
    assert _load_account(database).name == source.name
    with closing(oracle_connect(database)) as connection, connection:
        assert (
            connection.execute("SELECT count(*) FROM executerecord WHERE account_id=?", (copied.id,)).fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM portfolioaccount WHERE account_id=?", (copied.id,)).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM executerecord WHERE account_id=1").fetchone()[0] == 1


def test_create_idempotency_replays_before_custom_validation(account_client) -> None:
    client, database = account_client
    first = client.post("/account/", headers={"Idempotency-Key": "create-1"}, json=_account_payload())
    assert first.status_code == 201, first.text
    invalid = _account_payload() | {"account_control_preset": "unknown"}
    replay = client.post("/account/", headers={"Idempotency-Key": "create-1"}, json=invalid)
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    assert len(native_store_call(database, "account_list", {})) == 1


@pytest.mark.parametrize("new_key", ["old-create", "new-create"])
def test_deleted_account_recreation_and_idempotent_retry_are_isolated(account_client, new_key) -> None:
    client, database = account_client
    old = client.post("/account/", headers={"Idempotency-Key": "old-create"}, json=_account_payload())
    assert old.status_code == 201, old.text
    old_id = old.json()["id"]
    assert client.delete(f"/account/{old_id}").status_code == 200
    assert native_store_call(database, "account_create_replay", {"idempotency_key": "old-create"}) is None

    payload = _account_payload() | {"name": "replacement"}
    created = client.post("/account/", headers={"Idempotency-Key": new_key}, json=payload)
    assert created.status_code == 201, created.text
    new_id = created.json()["id"]
    assert new_id > old_id
    replay = client.post(
        "/account/",
        headers={"Idempotency-Key": new_key},
        json=payload | {"account_control_preset": "unknown"},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == new_id
    assert len(native_store_call(database, "account_list", {})) == 1
    if new_key != "old-create":
        assert native_store_call(database, "account_create_replay", {"idempotency_key": "old-create"}) is None


def test_runtime_registration_failure_rolls_back_create_and_update(account_client) -> None:
    client, database = account_client
    _seed_account(database)
    with closing(oracle_connect(database)) as connection, connection:
        connection.executescript(
            "CREATE TRIGGER reject_sync_insert BEFORE INSERT ON accountruntimesync BEGIN SELECT RAISE(ABORT,'blocked'); END;"
            "CREATE TRIGGER reject_sync_update BEFORE UPDATE ON accountruntimesync BEGIN SELECT RAISE(ABORT,'blocked'); END;"
        )
    assert client.post("/account/", json=_account_payload() | {"name": "failed"}).status_code == 500
    assert client.patch("/account/1", json={"name": "failed", "portfolio_id": -1}).status_code == 500
    saved = _load_account(database)
    assert saved.name == "ctp-testnet-sim" and saved.portfolio_id == 1
    with closing(oracle_connect(database)) as connection, connection:
        for table in ("account", "account_settings", "portfolioaccount"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1


def test_late_update_failure_rolls_back_connection_supplements_intent_and_outbox(account_client) -> None:
    from axile.server.connection_state import connection_config_key
    from axile.server.execution.supplements import fingerprint

    client, database = account_client
    account = _seed_account(database, supplement={"count": 2, "interval_minutes": 1})
    base = "2026-10-23T10:00:00+08:00"
    native_store_call(
        database,
        "supplement_create_group",
        {
            "account_id": 1,
            "group": {
                "id": "rollback-group",
                "fingerprint": fingerprint(account),
                "base_scheduled_at": base,
                "expires_at": "2026-10-23T10:15:00+08:00",
                "configured_count": 2,
                "effective_count": 1,
            },
            "steps": [
                {"id": "rollback-step", "group_id": "rollback-group", "index": 0, "scheduled_at": base},
                {
                    "id": "pending-step",
                    "group_id": "rollback-group",
                    "index": 1,
                    "scheduled_at": "2026-10-23T10:01:00+08:00",
                },
            ],
        },
    )
    native_store_call(
        database,
        "intent_insert",
        {
            "execution_id": "rollback-intent",
            "account_id": 1,
            "kind": "rebalance",
            "trigger_source": "scheduler",
            "channel": "ctp",
            "algorithm": "SINGLE-MAKER",
            "payload": {"supplement_step_id": "rollback-step"},
            "created_at": base,
            "now": base,
            "calendar_status_by_channel": {"ctp": "available_open"},
        },
    )
    with closing(oracle_connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO account_connection_state(account_id,config_key,instance_id,sequence,status,manually_disconnected) "
            "VALUES(1,?,'active-worker',4,'ok',0)",
            (connection_config_key(account.trade_channel, account.account_config),),
        )
        connection.executescript(
            "CREATE TRIGGER reject_sync_update BEFORE INSERT ON accountruntimesync "
            "BEGIN SELECT RAISE(ABORT,'late runtime failure'); END;"
        )
    tables = (
        "account",
        "account_settings",
        "portfolioaccount",
        "accountruntimesync",
        "account_connection_state",
        "supplement_group",
        "supplement_step",
        "execution_intent",
        "notification_event",
    )

    def snapshot():
        with closing(oracle_connect(database)) as connection, connection:
            return {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() for table in tables}

    before = snapshot()
    response = client.patch(
        "/account/1",
        json={
            "name": "failed",
            "portfolio_id": -1,
            "is_started": False,
            "account_config": {"password": "replacement"},
        },
    )
    assert response.status_code == 500, response.text
    assert "late runtime failure" in response.json()["detail"]
    assert snapshot() == before


@pytest.mark.parametrize("reject_delete", [False, True])
def test_native_delete_cleans_owned_state_atomically(native_database, reject_delete) -> None:
    for ident in (1, 2):
        _seed_account(native_database, name=f"account-{ident}", portfolio_id=None, is_started=False)
    with closing(oracle_connect(native_database)) as connection, connection:
        for ident in (1, 2):
            connection.execute(
                "INSERT INTO account_notification_state(account_id,execution_id) VALUES(?,?)", (ident, f"e{ident}")
            )
            connection.execute(
                "INSERT INTO account_connection_state(account_id,config_key,instance_id,sequence,status,manually_disconnected) VALUES(?,'key','',0,'unknown',0)",
                (ident,),
            )
            connection.execute(
                "INSERT INTO accountruntimesyncattempt(account_id,revision,succeeded,attempted_at) VALUES(?,1,0,'now')",
                (ident,),
            )
            connection.execute("INSERT INTO order_submission_monitor(account_id,data) VALUES(?,'{}')", (ident,))
            connection.execute(
                "INSERT INTO order_submission(submission_uid,account_id,occurred_at_ms,revision,data) VALUES(?,?,1,1,'{}')",
                (f"o{ident}", ident),
            )
            connection.execute(
                "INSERT INTO order_submission_warning(event_uid,account_id,occurred_at_ms,data) VALUES(?,?,1,'{}')",
                (f"w{ident}", ident),
            )
            connection.execute(
                "INSERT INTO account_control_event(account_id,control_date,execution_id,seq,channel,operation,metadata,decision,counted,outcome,event_uid,created_at,occurred_at_ms) VALUES(?,'day',?,1,'ctp','submit','{}','allow',1,'ok',?,'now',1)",
                (ident, f"e{ident}", f"c{ident}"),
            )
            connection.execute(
                "INSERT INTO account_control_counter_delta(account_id,execution_id,control_date,bucket_type,bucket_start,scope_type,operation,delta_count,delta_uid) VALUES(?,?,'day','day','day','account','submit',1,?)",
                (ident, f"e{ident}", f"d{ident}"),
            )
            connection.execute(
                "INSERT INTO schedule_skip(account_id,channel,triggered_at,calendar_id,calendar_day,calendar_label,reason_code) VALUES(?,'ctp','now','test','day','test','test')",
                (ident,),
            )
            connection.execute(
                "INSERT INTO supplement_group(id,account_id,fingerprint,base_scheduled_at,expires_at,configured_count,effective_count,status) VALUES(?,?,'key','now','later',1,1,'active')",
                (f"g{ident}", ident),
            )
            connection.execute(
                "INSERT INTO supplement_step(id,group_id,\"index\",scheduled_at,status) VALUES(?,?,0,'now','pending')",
                (f"s{ident}", f"g{ident}"),
            )
            connection.execute(
                "INSERT INTO notification_event(id,account_id,created_at,event_type,context,status) VALUES(?,?,'now','execution.finished',?,'pending')",
                (f"n{ident}", ident, '{"sentinel": ' + str(ident) + "}"),
            )
            connection.execute(
                "INSERT INTO execution_intent(execution_id,account_id,kind,trigger_source,status,payload,created_at) VALUES(?,?,'REBALANCE','manual','QUEUED','{}','now')",
                (f"e{ident}", ident),
            )
            connection.execute(
                "INSERT INTO executerecord(account_id,execution_id,is_success,created_at,facts_complete,audit_evidence,termination_evidence,result_input_kind,termination_kind,termination_cancel_ids_kind) VALUES(?,?,1,'now',0,'{}','{}','absent','absent','absent')",
                (ident, f"e{ident}"),
            )
            connection.execute(
                "INSERT INTO account_asset_snapshot(account_id,source,created_at) VALUES(?,'manual','now')", (ident,)
            )
            connection.execute(
                "INSERT INTO account_control_session(instance_id,account_id,execution_id,context_kind,source,timezone,started_at_ms,status) VALUES(?,?,?,'unknown','unknown','Asia/Shanghai',1,'active')",
                (f"i{ident}", ident, f"e{ident}"),
            )
            connection.execute(
                "INSERT INTO execution_event(account_id,execution_id,channel,algorithm,schema_version,event_uid,event_type,status,reason_family,reason_code,ts_local_created,ts_persisted,seq,details) VALUES(?,?,'ctp','SINGLE-MAKER',1,?,'ORDER_SUBMITTED','INFO','SYSTEM','TEST','now','now',0,'{}')",
                (ident, f"e{ident}", f"event{ident}"),
            )
            connection.execute(
                "INSERT INTO execution_artifact(execution_id,artifact_type,schema_version,content,created_at) VALUES(?,'TARGET_SNAPSHOT',1,?,'now')",
                (f"e{ident}", '{"sentinel": ' + str(ident) + "}"),
            )
    expected = {
        "account": ("id,name", [(2, "account-2")]),
        "account_settings": ("account_id", [(2,)]),
        "portfolioaccount": ("account_id", [(2,)]),
        "account_notification_state": ("account_id", [(2,)]),
        "account_connection_state": ("account_id", [(2,)]),
        "account_asset_snapshot": ("account_id,source", [(2, "manual")]),
        "executerecord": ("account_id,execution_id", [(2, "e2")]),
        "supplement_group": ("id,account_id", [("g2", 2)]),
        "supplement_step": ("id,group_id", [("s2", "g2")]),
        "accountruntimesync": ("account_id,revision", [(2, 1)]),
        "accountruntimesyncattempt": ("account_id,revision", [(2, 1)]),
        "execution_intent": ("account_id,execution_id", [(2, "e2")]),
        "notification_event": ("account_id,id,context", [(2, "n2", '{"sentinel": 2}')]),
        "account_control_session": ("account_id,instance_id", [(2, "i2")]),
        "account_control_event": ("account_id,event_uid", [(2, "c2")]),
        "account_control_counter_delta": ("account_id,delta_uid", [(2, "d2")]),
        "order_submission": ("account_id,submission_uid", [(2, "o2")]),
        "order_submission_monitor": ("account_id", [(2,)]),
        "order_submission_warning": ("account_id,event_uid", [(2, "w2")]),
        "schedule_skip": ("account_id", [(2,)]),
        "execution_event": ("account_id,event_uid", [(2, "event2")]),
        "execution_artifact": ("execution_id,artifact_type,content", [("e2", "TARGET_SNAPSHOT", '{"sentinel": 2}')]),
    }
    with closing(oracle_connect(native_database)) as connection, connection:
        before = {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() for table in expected}
        if reject_delete:
            connection.execute(
                "CREATE TRIGGER reject_account_delete AFTER DELETE ON account "
                "BEGIN SELECT RAISE(ABORT,'delete rejected'); END"
            )
    if reject_delete:
        with pytest.raises(_core.NativeError, match="delete rejected"):
            native_store_call(native_database, "account_delete", {"account_id": 1})
    else:
        native_store_call(native_database, "account_delete", {"account_id": 1})
    with closing(oracle_connect(native_database)) as connection, connection:
        for table, (columns, rows) in expected.items():
            if reject_delete:
                assert connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() == before[table], table
            else:
                assert connection.execute(f'SELECT {columns} FROM "{table}" ORDER BY 1').fetchall() == rows, table
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_update_rejects_stale_credential_merge(account_client, monkeypatch) -> None:
    client, database = account_client
    source = _seed_account(database)
    operation = account_crud_routes.native_operation

    async def concurrent_update(name, request):
        if name == "account_update":
            native_store_call(
                database,
                "account_update",
                {
                    "account_id": 1,
                    "patch": {"account_config": source.account_config | {"password": "concurrent-secret"}},
                    "now": "2026-10-23T09:00:01",
                },
            )
        return await operation(name, request)

    monkeypatch.setattr(account_crud_routes, "native_operation", concurrent_update)
    response = client.patch("/account/1", json={"account_config": {"td_front": "tcp://new:1"}})
    assert response.status_code == 409, response.text
    saved = _load_account(database)
    assert saved.account_config["password"] == "concurrent-secret"
    assert saved.account_config["td_front"] == "tcp://td:1"
    assert saved.runtime_sync.revision == 2


def test_copy_rejects_concurrently_changed_source_configuration(account_client, monkeypatch) -> None:
    client, database = account_client
    _seed_account(database, is_started=False)
    operation = account_crud_routes.native_operation

    async def concurrent_update(name, request):
        if name == "account_copy":
            native_store_call(
                database,
                "account_update",
                {
                    "account_id": 1,
                    "patch": {
                        "trade_channel": "gm",
                        "account_config": {
                            "connection_mode": "service",
                            "account_id": "gm-account",
                            "token": "new-secret",
                            "serv_addr": "127.0.0.1:7001",
                        },
                    },
                    "now": "2026-10-23T09:00:01",
                },
            )
        return await operation(name, request)

    monkeypatch.setattr(account_crud_routes, "native_operation", concurrent_update)
    response = client.post("/account/1/copy", json={"name": "新账户"})
    assert response.status_code == 409, response.text
    assert len(native_store_call(database, "account_list", {})) == 1
    assert _load_account(database).account_config["token"] == "new-secret"


def test_copy_normalizes_legacy_blank_notification_without_changing_source(account_client) -> None:
    client, database = account_client
    _seed_account(database, is_started=False, execution_notification_code="  ")
    response = client.post("/account/1/copy", json={"name": "新账户"})
    assert response.status_code == 201, response.text
    assert _load_account(database, response.json()["id"]).execution_notification_code is None
    assert _load_account(database).execution_notification_code == "  "
