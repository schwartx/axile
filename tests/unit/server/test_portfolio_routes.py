"""自定义函数组合路由契约测试。"""

import asyncio

import pytest

from axile.server.portfolio_function import _normalize_target

CODE = "def calculate_portfolio(context):\n    return {'rb2610': 1.0}\n"


@pytest.mark.parametrize(
    "target",
    [[], {1: 0.5}, {"rb2610": True}, {"rb2610": "0.5"}, {"rb2610": float("inf")}],
)
def test_target_must_be_finite_symbol_weight_mapping(target: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _normalize_target(target)


def _account_payload(account_id: int) -> dict[str, object]:
    return {
        "id": account_id,
        "name": "account",
        "market": "futures",
        "trade_channel": "ctp",
        "account_control_preset": "default",
        "account_config": {},
        "is_started": False,
        "cron_expr": "",
        "remark": None,
        "brokerage": "ctp",
        "weight_precision": 0.01,
        "algorithm": {"method": "SINGLE-MAKER", "params": {}},
        "trade_rules": {},
        "forbidden_symbols": [],
        "risk_symbols": [],
        "feishu_key": None,
    }


@pytest.mark.parametrize("account_id", [None, 7])
@pytest.mark.parametrize("valid", [True, False])
def test_run_and_save_persists_only_successful_trial(monkeypatch, account_id, valid):
    from unittest.mock import AsyncMock, Mock

    from axile.server.api.routes import portfolio as routes
    from axile.server.db.models import Portfolio, ValidateCustomCalcResponse
    from axile.server.db.models.account import AccountSnapshot
    from axile.server.db.models.portfolio import RunAndSavePortfolioRequest

    portfolio = Portfolio(id=3, name="before", market="futures", custom_calc_py_code=CODE + "# original\n")
    account = _account_payload(account_id) if account_id is not None else None
    stages = []

    async def native(name, request):
        assert name == "portfolio_operation"
        stages.append(request["op"])
        if request["op"] == "context":
            return {"portfolio": portfolio.model_dump(), "account_id": account_id, "account": account}
        assert stages == ["context", "run", "save_trial"]
        assert request["portfolio_id"] == 3
        assert request["changes"] == {"name": "after", "custom_calc_py_code": CODE}
        assert request["raw_weights"] == {"rb2610": 1.0}
        assert request["normalized_weights"] is None
        assert request["account_id"] == account_id
        assert request["source"] == "manual"
        assert request["updated_at"] == request["calculated_at"]

    async def trial(context, code):
        assert stages == ["context"]
        assert (context is None) is (account_id is None)
        if context is not None:
            assert isinstance(context, AccountSnapshot) and context.id == account_id
        assert code == CODE
        stages.append("run")
        return ValidateCustomCalcResponse(valid=valid, target={"rb2610": 1.0} if valid else None)

    run = AsyncMock(side_effect=trial)
    clear = Mock()
    monkeypatch.setattr(routes, "native_operation", native)
    monkeypatch.setattr(routes, "_run_custom_calc_validation", run)
    monkeypatch.setattr(routes, "try_register_target_refresh", Mock(return_value=True))
    monkeypatch.setattr(routes, "clear_target_refresh", clear)

    result = asyncio.run(
        routes.run_and_save_portfolio(
            3, RunAndSavePortfolioRequest(name="after", custom_calc_py_code=CODE, account_id=account_id)
        )
    )

    assert result.valid is valid
    expected = ["context", "run", "save_trial"] if valid else ["context", "run"]
    assert stages == expected
    run.assert_awaited_once()
    clear.assert_called_once_with(3, account_id)
    assert portfolio.name == "before"


def test_run_and_save_releases_refresh_reservation_when_native_save_fails(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from axile.server.api.routes import portfolio as routes
    from axile.server.db.models import ValidateCustomCalcResponse
    from axile.server.db.models.portfolio import RunAndSavePortfolioRequest

    native = AsyncMock(side_effect=[{"account": None}, RuntimeError("write failed")])
    clear = Mock()
    monkeypatch.setattr(routes, "native_operation", native)
    monkeypatch.setattr(
        routes,
        "_run_custom_calc_validation",
        AsyncMock(return_value=ValidateCustomCalcResponse(valid=True, target={"rb2610": 1.0})),
    )
    monkeypatch.setattr(routes, "try_register_target_refresh", Mock(return_value=True))
    monkeypatch.setattr(routes, "clear_target_refresh", clear)
    with pytest.raises(RuntimeError, match="write failed"):
        asyncio.run(
            routes.run_and_save_portfolio(3, RunAndSavePortfolioRequest(name="after", custom_calc_py_code=CODE))
        )
    clear.assert_called_once_with(3, None)


def test_portfolio_native_errors_keep_http_status_and_details(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from axile.server.api.routes import portfolio as routes

    for code, http_status, detail in [
        ("NOT_FOUND", 404, "组合不存在"),
        ("CONFLICT", 422, "该组合与账户关联，必须解绑才能删除组合"),
    ]:
        monkeypatch.setattr(routes, "native_operation", AsyncMock(side_effect=routes._core.NativeError(code, detail)))
        with pytest.raises(HTTPException) as caught:
            asyncio.run(routes.delete_portfolio(3))
        assert caught.value.status_code == http_status
        assert caught.value.detail == detail


def test_portfolio_queries_forward_native_json_without_decoding(monkeypatch):
    from unittest.mock import AsyncMock

    from axile.server.api.routes import portfolio as routes

    body = b'{"data": []}'
    native = AsyncMock(return_value=body)
    monkeypatch.setattr(routes, "native_operation", native)
    response = asyncio.run(routes.list_portfolios())
    assert response.body == body
    assert response.media_type == "application/json"
    native.assert_awaited_once_with("portfolio_query_json", {"op": "list"})


def test_portfolio_write_receipts_keep_persisted_fields_status_and_errors(native_database, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axile.server.api.routes import portfolio as routes

    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        created = client.post("/portfolio/", json={"name": "组合", "market": "期货", "custom_calc_py_code": CODE})
        assert created.status_code == 201, created.text
        value = created.json()
        identity = value["id"]
        assert value["account_id"] is None and value["description"] is None
        assert value["status"] is None and value["tag"] is None
        assert value["custom_calc_py_code"] == CODE
        detail = f"/portfolio/{identity}"
        assert created.content == client.get(detail).content
        assert client.patch(detail, json={"custom_calc_py_code": None}).status_code == 422
        assert client.patch(detail, json={"custom_calc_py_code": " "}).status_code == 422
        updated = client.patch(detail, json={"name": "更新后", "description": "说明", "status": "active", "tag": "tag"})
        assert updated.status_code == 200
        assert updated.json()["name"] == "更新后" and updated.json()["description"] == "说明"
        assert updated.content == client.get(detail).content
        assert client.patch(detail, json={}).content == updated.content
        cleared = client.patch(detail, json={"description": None, "status": None, "tag": None})
        assert cleared.status_code == 200
        assert all(cleared.json()[field] is None for field in ("description", "status", "tag"))
        assert cleared.json()["name"] == "更新后" and cleared.json()["market"] == "期货"
        deleted = client.delete(detail)
        assert deleted.status_code == 200 and deleted.json() == {"message": "成功删除组合"}
        assert client.get(detail).status_code == client.patch(detail, json={}).status_code == 404
        assert client.delete(detail).status_code == 404


@pytest.mark.parametrize("field", ["name", "market"])
def test_portfolio_patch_rejects_null_required_fields(native_database, monkeypatch, field):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axile.server.api.routes import portfolio as routes

    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(native_database))
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        created = client.post("/portfolio/", json={"name": "组合", "market": "期货", "custom_calc_py_code": CODE})
        assert created.status_code == 201, created.text
        detail = f"/portfolio/{created.json()['id']}"
        response = client.patch(detail, json={field: None, "description": "不可写入"})
        assert response.status_code == 422, response.text
        assert response.json()["detail"][0]["loc"] == ["body", field]
        assert client.get(detail).content == created.content


@pytest.mark.parametrize("field", ["name", "market"])
def test_native_portfolio_patch_rejects_null_required_fields_without_writes(native_database, field):
    from axile.server._native import _core
    from tests.conftest import native_store_call

    created = native_store_call(
        native_database,
        "portfolio_operation",
        {
            "op": "create",
            "portfolio": {
                "name": "组合",
                "market": "期货",
                "custom_calc_py_code": CODE,
                "created_at": "before",
                "updated_at": "before",
            },
        },
    )
    identity = created["id"]
    with pytest.raises(_core.NativeError) as caught:
        native_store_call(
            native_database,
            "portfolio_operation",
            {
                "op": "update",
                "portfolio_id": identity,
                "changes": {field: None, "description": "不可写入"},
                "updated_at": "after",
            },
        )
    assert caught.value.args[0] == "VALIDATION"
    saved = native_store_call(native_database, "portfolio_operation", {"op": "context", "portfolio_id": identity})
    assert saved["portfolio"] == {key: value for key, value in created.items() if key != "account_id"}
