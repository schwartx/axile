"""品种固定算法的边界校验、持久化与调仓分派。"""

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session, SQLModel

from axile.channels import get_channel
from axile.channels.algorithm_scopes import futures_algorithm_scope, normalize_algorithm_scopes
from axile.common.order_param_model import OrderParamModel
from axile.domain.execution import ExecutionKind
from axile.executor.algorithms.core.base import register_algorithm
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.server.api.routes import account_crud, capabilities
from axile.server.db.models import Account, AccountCreate, AccountSnapshot
from axile.server.db.models.account_settings import ExecutionSettings
from axile.server.execution.execution_algorithms import resolve_execution_algorithm_name
from axile.server.execution.rebalance import _build_rebalance_standard_input
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_account_control_routes import (
    _account_payload,
    _build_app,
    _noop_async,
    _RouteSession,
    _synchronized_runtime_sync,
)

PROFILE = {"algorithm": {"method": "TWAP", "params": {"total_duration": 30}}}


@pytest.mark.parametrize("symbol", ["ag", "AG", "ag2612", "SHFE.ag2701", "shfe.AG2612"])
def test_futures_scope_matches_all_months(symbol):
    assert futures_algorithm_scope(symbol) == "SHFE.ag"


@pytest.mark.parametrize("symbol", ["unknown", "DCE.ag", "ag2612C6000", "SP ag2612&ag2701"])
def test_invalid_futures_scope_is_rejected(symbol):
    with pytest.raises(ValueError):
        futures_algorithm_scope(symbol)


def test_product_profile_validates_format_duplicates_params_and_channel():
    payload = _account_payload()
    created = AccountCreate(**payload, product_algorithms={"AG": PROFILE})
    assert set(created.product_algorithms) == {"SHFE.ag"}
    for products in [
        {"ag2612": PROFILE},
        {"unknown": PROFILE},
        {"ag": PROFILE, "SHFE.ag": PROFILE},
        {"ag": {"algorithm": {"method": "TWAP", "params": {"total_duration": -1}}}},
        {"ag": {"algorithm": {}}},
        {"ag": {**PROFILE, "algorithm_schedule": []}},
        None,
    ]:
        with pytest.raises(ValidationError):
            AccountCreate(**payload, product_algorithms=products)
    with pytest.raises(ValidationError, match="未适配"):
        AccountSnapshot.model_validate(
            {
                **payload,
                "product_algorithms": {"ag": {"algorithm": {"method": "TEST-PRODUCT-POS-ONLY", "params": {}}}},
            }
        )


@register_algorithm("TEST-PRODUCT-POS-ONLY", order_param_models=[OrderParamModel.POSITION_SIDE])
def _position_side_only(executor, algorithm_input):
    raise NotImplementedError


def test_gm_scope_uses_full_security_identity():
    assert normalize_algorithm_scopes(get_channel("gm"), {"SHSE.600000": PROFILE}) == {"600000.SH": PROFILE}
    with pytest.raises(ValueError, match="重复"):
        normalize_algorithm_scopes(get_channel("gm"), {"SHSE.600000": PROFILE, "600000.SH": PROFILE})


def test_product_settings_roundtrip_copy_and_clear_preserve_other_settings():
    engine = sa.create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    payload = _account_payload() | {"id": 1, "product_algorithms": {"SHFE.ag": PROFILE}}
    with Session(engine) as session:
        session.add(Account(**payload))
        session.commit()
    with Session(engine) as session:
        account = session.get(Account, 1)
        assert account.product_algorithms["SHFE.ag"].algorithm == PROFILE["algorithm"]
        assert account.snapshot().product_algorithms["SHFE.ag"].algorithm == PROFILE["algorithm"]
        copied = AccountCreate(
            **(
                account.model_dump(
                    exclude={"id", "copied_from_account_id", "copied_from_account_name", "updated_at", "created_at"}
                )
                | {"name": "副本"}
            )
        )
        copied.product_algorithms["SHFE.ag"].algorithm["params"]["total_duration"] = 60
        assert account.product_algorithms["SHFE.ag"].algorithm == PROFILE["algorithm"]
        account.sqlmodel_update({"name": "改名"})
        assert account.product_algorithms
        account.sqlmodel_update({"product_algorithms": {}})
        session.commit()
    with Session(engine) as session:
        account = session.get(Account, 1)
        assert account.product_algorithms == {}
        assert account.algorithm == payload["algorithm"]
        assert account.name == "改名"
    assert ExecutionSettings.model_validate({"algorithm": payload["algorithm"]}).product_algorithms == {}
    engine.dispose()


def test_rebalance_and_worker_roundtrip_keep_full_product_algorithm():
    account = build_account(product_algorithms={"SHFE.ag": PROFILE})
    account.algorithm = {"method": "SINGLE-MAKER", "params": {"timeout": 99}}
    standard_input = _build_rebalance_standard_input(
        account=account,
        curr_target={"ag2612": 0.1, "ag2701": 0.1, "rb2610": 0.1},
        last_target={"ag2606": 0.1},
        execution_id="product-test",
        trigger_source="scheduler",
    )
    restored = UnifiedStandardInput.from_dict(standard_input.to_dict())
    for symbol in ["ag2612", "ag2701", "SHFE.ag2606"]:
        assert restored.get_symbol_algorithm(symbol) == PROFILE["algorithm"]
    assert restored.get_symbol_algorithm("rb2610") == account.algorithm
    assert resolve_execution_algorithm_name(account, ExecutionKind.REBALANCE) == "按品种算法"
    assert resolve_execution_algorithm_name(account, ExecutionKind.CLEAR_POSITIONS) != "按品种算法"
    # Full config and profile map must remain frozen after the account changes.
    account.product_algorithms = {}
    assert standard_input.get_symbol_algorithm("ag2612") == PROFILE["algorithm"]
    with pytest.raises(ValueError, match="无法识别"):
        restored.get_symbol_algorithm("unknown2612")
    with pytest.raises(ValidationError, match="完整"):
        UnifiedStandardInput.from_dict({**standard_input.to_dict(), "product_algorithms": {"ag": {"method": "TWAP"}}})


def test_scope_endpoint_normalizes_and_rejects_invalid_input():
    app = FastAPI()
    app.include_router(capabilities.router)
    client = TestClient(app)
    assert client.get("/capabilities/channels/ctp/algorithm-scope", params={"code": "AG"}).json() == {"code": "SHFE.ag"}
    assert client.get("/capabilities/channels/ctp/algorithm-scope", params={"code": "ag2612"}).status_code == 422
    assert client.get("/capabilities/channels/missing/algorithm-scope", params={"code": "ag"}).status_code == 404
    ctp = next(item for item in client.get("/capabilities/channels").json() if item["channel"] == "ctp")
    assert ctp["algorithm_scope"]["example"] == "ag"


def test_patch_product_algorithms_normalizes_preserves_and_clears(monkeypatch):
    monkeypatch.setattr(account_crud, "enqueue_account_runtime_sync", _noop_async)
    monkeypatch.setattr(account_crud, "_reconcile_committed_runtime", _synchronized_runtime_sync)
    session = _RouteSession(build_account())
    client = TestClient(_build_app(session))
    response = client.patch("/account/1", json={"product_algorithms": {"AG": PROFILE}})
    assert response.status_code == 200, response.text
    assert response.json()["product_algorithms"] == {"SHFE.ag": PROFILE}
    assert client.patch("/account/1", json={"name": "新名称"}).json()["product_algorithms"] == {"SHFE.ag": PROFILE}
    for products in [{"ag": PROFILE, "SHFE.ag": PROFILE}, {"unknown": PROFILE}, None]:
        assert client.patch("/account/1", json={"product_algorithms": products}).status_code == 422
        assert session.account.product_algorithms["SHFE.ag"].algorithm == PROFILE["algorithm"]
    assert client.patch("/account/1", json={"product_algorithms": {}}).json()["product_algorithms"] == {}
    assert session.account.algorithm == {"method": "SINGLE-MAKER", "params": {}}
