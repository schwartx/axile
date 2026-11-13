"""渠道品种校验、独立时段选择和真实存储回归。"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from axile.channels.algorithm_scopes import futures_algorithm_scope
from axile.domain.execution import ExecutionKind
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.server.db.models.account import Account, AccountUpdate
from axile.server.execution import execution_algorithms
from axile.server.execution.rebalance import _build_rebalance_standard_input
from tests.conftest import native_store_call
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_account_settings import account_data


def profile(slices=3):
    return {
        "algorithm": {"method": "TWAP", "params": {"slices": slices}},
        "algorithm_schedule": [
            {"start": "21:00", "end": "02:30", "algorithm": {"method": "TWAP", "params": {"slices": 8}}}
        ],
    }


@pytest.mark.parametrize(
    "channel,raw,expected",
    [
        ("ctp", " AG ", "SHFE.ag"),
        ("tq", "shfe.ag", "SHFE.ag"),
        ("ctp", "ta", "CZCE.TA"),
        ("gm", "shse.600000", "600000.SH"),
    ],
)
def test_channel_normalizes_scope(channel, raw, expected):
    account = Account.model_validate(
        build_account().model_dump() | {"trade_channel": channel, "product_algorithms": {raw: profile()}}
    )
    assert list(account.product_algorithms) == [expected]


@pytest.mark.parametrize(
    "channel,key",
    [
        ("ctp", "ag2612"),
        ("ctp", "AG*"),
        ("ctp", "NOTREAL"),
        ("ctp", "DCE.ag"),
        ("gm", "ag"),
        ("gm", "600000"),
        ("tq", "ag2612C5000"),
    ],
)
def test_channel_rejects_invalid_scope(channel, key):
    with pytest.raises(ValidationError):
        Account.model_validate(
            build_account().model_dump() | {"trade_channel": channel, "product_algorithms": {key: profile()}}
        )


def test_alias_duplicates_channel_changes_and_nested_schedules_rejected():
    account = build_account()
    with pytest.raises(ValidationError, match="重复"):
        Account.model_validate(account.model_dump() | {"product_algorithms": {"ag": profile(), "SHFE.ag": profile()}})
    configured = Account.model_validate(account.model_dump() | {"product_algorithms": {"ag": profile()}})
    with pytest.raises(ValidationError, match="格式"):
        Account.model_validate(configured.model_dump() | {"trade_channel": "gm"})
    overlapping = profile()
    overlapping["algorithm_schedule"] *= 2
    with pytest.raises(ValidationError, match="重叠"):
        AccountUpdate(product_algorithms={"ag": overlapping})
    with pytest.raises(ValidationError):
        AccountUpdate(product_algorithms={"ag": profile(slices=0)})
    assert AccountUpdate(remark="only").model_dump(exclude_unset=True) == {"remark": "only"}


@pytest.mark.parametrize("time,slices", [("01:00", 8), ("02:30", 3), ("09:30", 3), ("21:00", 8)])
def test_independent_product_schedule_freezes_and_survives_input_roundtrip(monkeypatch, time, slices):
    account = Account.model_validate(
        build_account().model_dump()
        | {
            "algorithm": {"method": "TWAP", "params": {"slices": 5}},
            "algorithm_schedule": [
                {"start": "09:00", "end": "10:00", "algorithm": {"method": "TWAP", "params": {"slices": 6}}}
            ],
            "product_algorithms": {"ag": profile()},
        }
    )
    calls = []

    def now(**_):
        calls.append(1)
        return datetime.fromisoformat(f"2026-11-13T{time}:00+08:00")

    monkeypatch.setattr(execution_algorithms, "clock_now", now)
    selected, evidence = execution_algorithms.select_rebalance_algorithm(account)
    assert execution_algorithms.resolve_execution_algorithm_name(selected, ExecutionKind.REBALANCE) == "按品种"
    assert len(calls) == 1
    standard = _build_rebalance_standard_input(
        account=selected,
        curr_target={"ag2612": 0.2, "ag2702": 0.1, "rb2701": 0.1},
        last_target={},
        execution_id="fixture",
        trigger_source="manual",
    )
    standard = UnifiedStandardInput.from_dict(standard.to_dict())
    for symbol in ("ag2612", "SHFE.ag2702"):
        assert standard.get_symbol_algorithm(symbol)["params"] == {"slices": slices}
    expected = 6 if time == "09:30" else 5
    assert standard.get_symbol_algorithm("rb2701")["params"] == {"slices": expected}
    assert standard.get_symbol_algorithm("ag2612C5000")["params"] == {"slices": expected}
    standard.symbol_algorithms = {"ag2612": {"method": "TWAP", "params": {"slices": 9}}}
    assert standard.get_symbol_algorithm("ag2612")["params"] == {"slices": 9}
    assert evidence["product_algorithms"]["SHFE.ag"]["start"] == ("21:00" if slices == 8 else None)
    selected.product_algorithms["SHFE.ag"].algorithm["params"]["slices"] = 99
    assert account.product_algorithms["SHFE.ag"].algorithm["params"]["slices"] == 3


def test_future_contracts_exclude_options_and_combinations():
    assert futures_algorithm_scope("CZCE.TA701") == "CZCE.TA"
    for symbol in ("ag2612C5000", "cu2701-C-80000", "SP ag2612&ag2702", "SHFE.ag2612&ag2702"):
        with pytest.raises(ValueError):
            futures_algorithm_scope(symbol)


def test_standard_input_rejects_malformed_product_map_and_accepts_default_params():
    account = Account.model_validate(
        build_account().model_dump()
        | {
            "product_algorithms": {"ag": {"algorithm": {"method": "TWAP"}, "algorithm_schedule": []}},
        }
    )
    selected, _ = execution_algorithms.select_rebalance_algorithm(account)
    standard = _build_rebalance_standard_input(
        account=selected, curr_target={"ag2612": 0.1}, last_target={}, execution_id="fixture", trigger_source="manual"
    )
    assert standard.get_symbol_algorithm("ag2612") == {"method": "TWAP", "params": {}}
    for invalid in ([], None, {"ag": "TWAP"}, {"ag": {"params": {}}}):
        with pytest.raises(ValidationError):
            UnifiedStandardInput.from_dict(standard.to_dict() | {"product_algorithms": invalid})


def test_http_roundtrip_and_invalid_patch_keeps_existing_configuration():
    from tests.futures_close_app import app, reset

    reset()
    with TestClient(app) as client:
        channels = client.get("/api/v1/capabilities/channels").json()
        assert (
            next(channel for channel in channels if channel["channel"] == "ctp")["algorithm_scope"]["example"] == "ag"
        )
        response = client.patch("/api/v1/account/1", json={"product_algorithms": {"ag": profile()}})
        assert response.status_code == 200, response.text
        assert response.json()["product_algorithms"] == {"SHFE.ag": profile()}
        assert client.patch("/api/v1/account/1", json={"remark": "keep"}).json()["product_algorithms"] == {
            "SHFE.ag": profile()
        }
        assert client.patch("/api/v1/account/1", json={"product_algorithms": {"ag2612": profile()}}).status_code == 422
        assert client.get("/api/v1/account/1").json()["product_algorithms"] == {"SHFE.ag": profile()}
        assert client.patch("/api/v1/account/1", json={"product_algorithms": {}}).json()["product_algorithms"] == {}


def test_native_create_copy_clear_and_runtime_revision(native_database):
    saved = native_store_call(
        native_database,
        "account_create",
        {"account": account_data() | {"is_started": False}, "now": "2026-11-13T10:00:00"},
    )["account"]
    assert saved["product_algorithms"] == {}
    identity = saved["id"]
    updated = native_store_call(
        native_database,
        "account_update",
        {"account_id": identity, "patch": {"product_algorithms": {"SHFE.ag": profile()}}, "now": "2026-11-13T10:01:00"},
    )
    assert updated["runtime_changed"] is False  # 只换算法不重建柜台连接。
    assert updated["account"]["runtime_sync"]["revision"] > saved["runtime_sync"]["revision"]
    assert updated["account"]["product_algorithms"] == {"SHFE.ag": profile()}
    copied = native_store_call(
        native_database, "account_copy", {"account_id": identity, "name": "product copy", "now": "2026-11-13T10:02:00"}
    )
    assert copied["product_algorithms"] == {"SHFE.ag": profile()}
    cleared = native_store_call(
        native_database,
        "account_update",
        {"account_id": identity, "patch": {"product_algorithms": {}}, "now": "2026-11-13T10:03:00"},
    )
    assert cleared["account"]["product_algorithms"] == {}
    assert native_store_call(native_database, "account_get", {"account_id": copied["id"]})["product_algorithms"] == {
        "SHFE.ag": profile()
    }
