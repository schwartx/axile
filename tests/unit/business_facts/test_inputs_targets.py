"""冻结输入与历史目标的存储契约。"""

import asyncio

import pytest

from axile.server._native import _core
from axile.server.business_facts import repository
from axile.server.db.models.target_weight_snapshot import TargetWeightSnapshot
from axile.server.execution.input_persistence import sanitize_execution_input
from axile.server.target_weight_snapshots import (
    append_target_weight_snapshot,
    get_latest_account_target_snapshot,
    get_latest_portfolio_target_snapshot,
    read_target_snapshot_weights,
)
from scripts.business_facts_contract import business_evidence
from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import (
    freeze_input,
    persist_result,
    read_result,
    rows,
    seed_account,
    sql,
)

_INPUT_TABLES = ("execution_input", "execution_input_target", "execution_input_symbol", "execution_input_plan")


def _read_input(path, input_id="input-1"):
    return native_store_call(path, "read_execution_input", {"input_id": input_id})


def _roundtrip(path, payload):
    receipt = freeze_input(path, payload)
    return _read_input(path, receipt["input_id"]), {name: rows(path, name) for name in _INPUT_TABLES}


def _seed_portfolio(path):
    seed_account(path)
    sql(
        path,
        "INSERT INTO portfolio (id,name,market,custom_calc_py_code,created_at,updated_at) VALUES (2,'oracle','期货','','2026-10-20','2026-10-20')",
    )


def test_inputs_keep_native_identity_targets_and_scoped_configuration(native_database):
    payload = {
        "strategy_target": {"CF2701": 0, "CF701": -0.05},
        "curr_target": {"CF2701": 0.0, "CF701": -0.15},
        "account_target": {"CF2701": None},
        "last_target": {},
        "quantities": {"CF701": -2},
        "risk_symbols": ["CF2701", "CF2701", None],
        "forbidden_symbols": [],
        "sizing_context": {"long_leverage": 0, "short_leverage": 3.0, "weight_precision": None, "native": "kept"},
        "algorithm": {"method": "TARGET-POS-TASK", "params": {"offset_priority": "今昨,开", "limit": 0}},
        "extra": {"audit": {"execution_id": "execution-1", "is_test": False}},
        "channel_type": "tq",
    }
    decoded, facts = _roundtrip(native_database, payload)
    assert business_evidence(decoded) == business_evidence(payload)
    assert business_evidence(list(decoded["curr_target"])) == business_evidence(["CF2701", "CF701"])
    assert business_evidence(decoded["strategy_target"]["CF2701"]) == business_evidence(0)
    assert business_evidence(decoded["curr_target"]["CF2701"]) == business_evidence(0)
    assert business_evidence(facts["execution_input"][0]["audit_evidence"].get("extensions", {})) == business_evidence(
        {}
    )
    assert facts["execution_input"][0]["audit_evidence"]["unrecognized_fields"]["target_conflicts"] == {
        "account_target": {"CF2701": None}
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"curr_target": None},
        {"curr_target": {}},
        {"curr_target": []},
        {"curr_target": {"rb2610": "0"}},
        None,
        False,
        0,
        "legacy",
        [],
    ],
)
def test_input_missing_null_empty_malformed_and_root_evidence(native_database, payload):
    assert business_evidence(_roundtrip(native_database, payload)[0]) == business_evidence(payload)


@pytest.mark.parametrize("payload", [{}, {"curr_target": None}, {"curr_target": {}}, {"curr_target": {"A": 0}}])
def test_read_input_reuses_header_without_changing_collection_states(native_database, monkeypatch, payload):
    expected, _ = _roundtrip(native_database, payload)
    calls = []

    async def read(name, request):
        calls.append((name, request))
        return native_store_call(native_database, name, request)

    monkeypatch.setattr(repository, "native_operation", read)
    assert asyncio.run(repository.read_execution_input("input-1")) == expected
    assert calls == [("read_execution_input", {"input_id": "input-1"})]


def test_read_input_rejects_mismatched_header_before_querying(native_database):
    freeze_input(native_database, {})
    record = persist_result(native_database, {}, execution_id="another-execution")
    sql(native_database, "UPDATE executerecord SET input_id='input-1' WHERE id=?", (record["id"],))
    with pytest.raises(_core.NativeError, match="input belongs to another account or execution"):
        read_result(native_database, record)


@pytest.mark.parametrize(
    "payload, missing_table, message",
    [
        ({"curr_target": {"A": 0}}, "execution_input_target", "incomplete frozen targets"),
        ({"risk_symbols": ["A"]}, "execution_input_symbol", "incomplete collection facts"),
        ({"quantities": {"A": 0}}, "execution_input_plan", "incomplete frozen quantity plan"),
    ],
)
def test_read_input_with_header_still_rejects_incomplete_children(native_database, payload, missing_table, message):
    freeze_input(native_database, payload)
    sql(native_database, f'DELETE FROM "{missing_table}" WHERE input_id=?', ("input-1",))
    with pytest.raises(_core.NativeError, match=message):
        _read_input(native_database)


def test_sanitation_is_identical_for_live_and_migration_and_does_not_mutate_input(native_database):
    payload = {
        "account_config": {"password": "synthetic-secret"},
        "feishu_key": "synthetic-key",
        "execution_notification_code": "synthetic-source",
        "feishu_account": {"id": 1},
        "curr_target": {"rb2610": 0},
    }
    decoded, _ = _roundtrip(native_database, payload)
    assert business_evidence(decoded) == business_evidence(sanitize_execution_input(payload))
    assert decoded["feishu_key"] == "***"
    assert "account_config" not in decoded
    assert "execution_notification_code" not in decoded
    assert payload["feishu_key"] == "synthetic-key"
    with pytest.raises(_core.NativeError, match="root type"):
        freeze_input(
            native_database, [{"curr_target": {"rb2610": 0}}], execution_id="unsupported", input_id="unsupported"
        )


def test_persist_replays_identity_and_selective_target_reads(native_database):
    payload = {"curr_target": {"rb2610": 0}, "sizing_context": {"weight_precision": 0.01}}
    receipt = freeze_input(native_database, payload)
    assert freeze_input(native_database, payload) == receipt
    assert business_evidence(_read_input(native_database)) == business_evidence(payload)
    record = persist_result(native_database, {}, execution_id="execution-1", is_success=1)
    target = native_store_call(native_database, "execution_read", {"op": "last_target", "account_id": 1})
    assert target == {"present": True, "target": {"rb2610": 0}}
    assert "last_target" not in _read_input(native_database)
    sizing = native_store_call(
        native_database, "execution_read", {"op": "target_sizing", "account_id": 1, "execution_id": "execution-1"}
    )
    assert sizing["target_context"] == {"weight_precision": 0.01}
    assert sizing["record_id"] == record["id"]
    with pytest.raises(_core.NativeError, match="differs"):
        freeze_input(native_database, {"curr_target": {}})
    seed_account(native_database, 2)
    with pytest.raises(_core.NativeError, match="another account"):
        freeze_input(native_database, payload, account_id=2)

    first = persist_result(native_database, {}, raw_input={"curr_target": None})["input_id"]
    second = persist_result(native_database, {}, raw_input={"curr_target": {}})["input_id"]
    assert first != second
    assert _read_input(native_database, first)["curr_target"] is None
    assert _read_input(native_database, second)["curr_target"] == {}
    for ordinal, target in enumerate((None, {})):
        execution_id = f"target-state-{ordinal}"
        freeze_input(native_database, {"curr_target": target}, execution_id=execution_id, input_id=execution_id)
        persist_result(native_database, {}, execution_id=execution_id, is_success=1)
        assert native_store_call(native_database, "execution_read", {"op": "last_target", "account_id": 1}) == {
            "present": True,
            "target": target,
        }
    freeze_input(native_database, {}, execution_id="missing-target", input_id="missing-target")
    persist_result(native_database, {}, execution_id="missing-target", is_success=1)
    assert native_store_call(native_database, "execution_read", {"op": "last_target", "account_id": 1}) == {
        "present": False,
        "target": None,
    }

    variant_one = freeze_input(
        native_database, payload, audit_payload={"curr_target": {"rb2610": 1}}, audit_input_id="variant-one"
    )["audit_id"]
    variant_two = freeze_input(
        native_database, payload, audit_payload={"curr_target": {"rb2610": 2}}, audit_input_id="variant-two"
    )["audit_id"]
    assert variant_one != variant_two
    owners = [
        row["owner_execution_id"]
        for row in rows(native_database, "execution_input")
        if row["id"] in (variant_one, variant_two)
    ]
    assert owners == ["execution-1", "execution-1"]
    ordered = {"curr_target": {"rb2610": 0, "ag2612": 1}}
    ordered_receipt = freeze_input(native_database, ordered, execution_id="ordered-target", input_id="ordered-target")
    assert (
        freeze_input(
            native_database,
            {"curr_target": {"ag2612": 1, "rb2610": 0}},
            execution_id="ordered-target",
            input_id="ordered-target",
        )
        == ordered_receipt
    )
    with pytest.raises(_core.NativeError, match="differs"):
        freeze_input(native_database, {"curr_target": {"rb2610": 1.0}, "sizing_context": {"weight_precision": 0.01}})


@pytest.mark.parametrize(
    "payload",
    [
        {"raw_weights": {"CF2701": 0, "CF701": -0.05}, "normalized_weights": {"CF2701": 0.0, "CF701": -0.15}},
        {"raw_weights": None, "normalized_weights": {}},
        {},
        {"raw_weights": [], "normalized_weights": {"rb2610": "0"}},
    ],
)
def test_calculated_snapshots_keep_weight_roles_and_container_states(native_database, payload):
    _seed_portfolio(native_database)
    saved = native_store_call(
        native_database,
        "target_operation",
        {
            "op": "append",
            "portfolio_id": 2,
            "account_id": 1,
            "source": "manual",
            "calculated_at": "2026-10-20",
            **payload,
        },
    )
    actual = {
        key: value for key, value in saved.items() if key in ("raw_weights", "normalized_weights", "audit_evidence")
    }
    assert business_evidence(actual) == business_evidence(payload)
    head = rows(native_database, "target_weight_snapshot")[0]
    assert "raw_weights" not in head
    assert "normalized_weights" not in head


def test_target_snapshot_services_explicitly_load_typed_maps(native_database, monkeypatch):
    _seed_portfolio(native_database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))

    async def check():
        snapshot = await append_target_weight_snapshot(
            portfolio_id=2,
            account_id=1,
            raw_weights={"CF2701": 0, "CF701": -0.05},
            normalized_weights={"CF2701": 0.0, "CF701": -0.15},
            source="manual",
        )
        assert snapshot.raw_weights == {"CF2701": 0, "CF701": -0.05}
        assert snapshot.normalized_weights == {"CF2701": 0.0, "CF701": -0.15}
        assert await get_latest_account_target_snapshot(1, 2) == snapshot
        assert await get_latest_portfolio_target_snapshot(2) == snapshot
        assert await read_target_snapshot_weights(account_id=1, execution_id=None) is None
        sql(
            native_database,
            "UPDATE target_weight_snapshot SET execution_id='snapshot-execution' WHERE id=?",
            (snapshot.id,),
        )
        assert await read_target_snapshot_weights(account_id=1, execution_id="snapshot-execution") == {
            "CF2701": 0.0,
            "CF701": -0.15,
        }
        assert {"raw_weights", "normalized_weights"}.isdisjoint(TargetWeightSnapshot.model_fields)
        assert {"raw_weights", "normalized_weights"}.isdisjoint(rows(native_database, "target_weight_snapshot")[0])

    asyncio.run(check())


def test_input_variants_keep_owner_anchor_without_unique_execution_alias(native_database):
    primary = freeze_input(
        native_database, {"curr_target": {}}, audit_payload={"curr_target": None}, audit_input_id="variant"
    )
    anonymous = persist_result(native_database, {}, raw_input={})["input_id"]
    heads = {row["id"]: row for row in rows(native_database, "execution_input")}
    assert heads[primary["input_id"]]["owner_execution_id"] == "execution-1"
    assert heads[primary["audit_id"]]["owner_execution_id"] == "execution-1"
    assert heads[primary["audit_id"]]["execution_id"] is None
    assert heads[anonymous]["owner_execution_id"] is None
    assert _read_input(native_database, primary["audit_id"])["curr_target"] is None
    assert "owner_execution_id" not in _read_input(native_database, primary["input_id"])


def test_input_primary_identity_cannot_point_at_a_different_owner(native_database):
    freeze_input(native_database, {})
    sql(native_database, "UPDATE execution_input SET owner_execution_id='another' WHERE id='input-1'")
    with pytest.raises(_core.NativeError, match="其他执行"):
        freeze_input(native_database, {})
    assert len(rows(native_database, "execution_input")) == 1
