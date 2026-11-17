"""真实原生事实贯通目标展示、持仓对账和仪表盘。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.common.serialization import to_payload
from axile.server.api.deps import get_scheduler
from axile.server.api.routes import account as account_routes
from axile.server.core import db
from tests.conftest import native_store_call
from tests.sqlite_oracle import to_us
from tests.unit.server._business_facts_test_support import persist_result, sql
from tests.unit.server._execution_test_support import build_account


@pytest.mark.parametrize("mode", ["weight", "quantity"])
@pytest.mark.parametrize("reuse_previous", [False, True])
@pytest.mark.parametrize("ready,current,expected", [(True, 0, 0), (True, 1, 1), (False, 0, None)])
def test_all_account_views_use_one_recorded_quantity(
    native_database, monkeypatch, mode, reuse_previous, ready, current, expected
):
    path = native_database
    monkeypatch.setattr(db, "database_path", lambda: str(path))
    stamp = "2026-11-17T10:00:00Z"
    native_store_call(
        path,
        "account_create",
        {"now": stamp, "account": to_payload(build_account(id=1, portfolio_id=None), mode="json")},
    )
    sql(
        path,
        "INSERT INTO portfolio(id,name,market,custom_calc_py_code,created_at_us,updated_at_us) VALUES(7,'p','期货','',?,?)",
        (to_us(stamp), to_us(stamp)),
    )
    sql(path, "INSERT INTO portfolioaccount(account_id,portfolio_id,created_at_us) VALUES(1,7,?)", (to_us(stamp),))
    raw = {"SM2701": 0.02}
    native_store_call(
        path,
        "target_operation",
        {
            "op": "append",
            "account_id": 1,
            "portfolio_id": 7,
            "raw_weights": raw,
            "normalized_weights": raw,
            "target_mode": mode,
            "source": "execution",
            "execution_id": "sized",
            "calculated_at": stamp,
        },
    )
    persist_result(
        path,
        {
            "status": "NOOP",
            "symbol_results": {
                "SM701": {
                    "status": "NOOP",
                    "sizing": {
                        "sizing_mode": mode,
                        "status": "SIZED" if ready else "UNAVAILABLE",
                        "reason_code": "COMMON.SIZING.BELOW_MIN_QUANTITY",
                        "raw_quantity": 0.2,
                        "target_quantity": 0 if ready else None,
                    },
                }
            },
        },
        execution_id="sized",
        raw_input={"sizing_context": {"weight_precision": 0.01}},
        created_at=stamp,
    )
    if reuse_previous:
        native_store_call(
            path,
            "target_operation",
            {
                "op": "append",
                "account_id": 1,
                "portfolio_id": 7,
                "raw_weights": raw,
                "normalized_weights": raw,
                "target_mode": mode,
                "source": "manual",
                "calculated_at": stamp,
            },
        )
    native_store_call(
        path,
        "asset_snapshot_write",
        {
            "account_id": 1,
            "created_at": stamp,
            "account_assets": {
                "total_asset": 1_000_000,
                "positions": []
                if current == 0
                else [{"symbol": "SM701", "direction": "多头", "volume": current, "market_value": 100_000}],
            },
        },
    )
    calls = []
    from axile.server.api.routes import account_crud

    native = account_crud.native_operation

    async def record(name, request):
        calls.append(name)
        return await native(name, request)

    monkeypatch.setattr(account_crud, "native_operation", record)
    app = FastAPI()
    app.include_router(account_routes.router)
    app.dependency_overrides[get_scheduler] = lambda: SimpleNamespace(get_job=lambda _: None)
    with TestClient(app) as client:
        target = client.get("/account/1/target_snapshot")
        assert target.status_code == 200, target.text
        target = target.json()
        assert target["quantities"] == ({"SM701": 0} if ready else None)
        plan = client.get("/account/1/rebalance_plan")
        assert plan.status_code == 200, plan.text
        plan = plan.json()
        assert plan["target_mode"] == mode
        assert plan["off_symbol_count"] == expected
        if ready:
            assert plan["rows"][0]["target_quantity"] == 0
            assert plan["rows"][0]["action"] == ("aligned" if current == 0 else "close")
        else:
            assert plan["rows"] == []
        calls.clear()
        dashboard = client.get("/account/dashboard")
        assert dashboard.status_code == 200, dashboard.text
        assert dashboard.json()["data"][0]["off_symbol_count"] == expected
        assert calls == ["account_dashboard_context"]
