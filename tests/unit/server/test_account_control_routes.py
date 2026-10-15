"""账户控制相关路由测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.executor.account_control.registry import (
    ensure_default_account_control_registry_bootstrapped,
    reset_default_account_control_registry_for_tests,
)
from axile.server.api.deps import HistoryPagination, get_db, get_scheduler
from axile.server.api.routes import account as account_routes
from axile.server.api.routes import account_crud as account_crud_routes
from axile.server.api.routes.account_performance import _account_performance
from axile.server.db.models import (
    Account,
    AccountAssetSnapshot,
    AccountCreate,
    AccountRuntimeSync,
    ExecuteRecord,
    ExecutionEvent,
    Portfolio,
    PortfolioAccount,
)


@pytest.fixture(autouse=True)
def _seed_account_control_registry() -> None:
    registry = reset_default_account_control_registry_for_tests()
    ensure_default_account_control_registry_bootstrapped()
    registry.freeze()


class _RouteSession:
    async def close(self) -> None:
        return None

    def __init__(self, account: Account | None = None) -> None:
        self.account = account
        self.rollback_count = 0

    async def get(self, _model: object, account_id: int) -> Account | None:
        if self.account is not None and self.account.id == account_id:
            return self.account
        return None

    def add(self, account: Account) -> None:
        self.account = account

    async def flush(self) -> None:
        if self.account is not None and self.account.id is None:
            self.account.id = 1

    async def execute(self, _statement: object) -> None:
        """配置更新同时执行连接状态的条件覆盖；实体细节由持久化测试验证。"""
        return None

    async def commit(self) -> None:
        return None

    async def refresh(self, _account: Account) -> None:
        return None

    async def rollback(self) -> None:
        self.rollback_count += 1


class _Scheduler:
    def get_job(self, _job_id: str) -> None:
        return None


def test_copy_account_keeps_secrets_server_side_and_source_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """复制仅转移配置，来源及执行历史不会随新账户对象复制。"""
    source = _build_account()
    source.is_started = False
    session = _RouteSession(source)
    bindings: list[tuple[int, int | None]] = []

    async def record_binding(_session: object, account_id: int, portfolio_id: int | None) -> None:
        bindings.append((account_id, portfolio_id))

    async def no_sync(*_args: object, **_kwargs: object) -> None:
        return None

    async def reconciled(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(status="synchronized")

    monkeypatch.setattr(account_crud_routes, "add_record_portfolio_account", record_binding)
    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", no_sync)
    monkeypatch.setattr(account_crud_routes, "_reconcile_committed_runtime", reconciled)
    monkeypatch.setattr(account_crud_routes, "_account_public_with_runtime_sync", lambda account, _sync: account)
    monkeypatch.setattr(account_crud_routes, "get_running_execution_id", lambda _id: None)
    monkeypatch.setattr(account_crud_routes, "get_queued_execution_id", lambda _id: None)

    copied = asyncio.run(
        account_crud_routes.copy_account(
            session, _Scheduler(), 1, account_crud_routes.AccountCopyRequest(name="新账户"), Response()
        )
    )
    assert copied is not source
    assert copied.name == "新账户"
    assert copied.is_started is False
    assert all(copied.account_config[key] == value for key, value in source.account_config.items())
    assert copied.copied_from_account_id == 1
    assert copied.copied_from_account_name == source.name
    assert source.name == "ctp-testnet-sim"
    assert source.copied_from_account_id is None
    assert bindings == [(1, source.portfolio_id)]


@pytest.mark.parametrize("condition", ["started", "scheduled", "running", "queued"])
def test_copy_account_rejects_active_source(monkeypatch: pytest.MonkeyPatch, condition: str) -> None:
    source = _build_account()
    source.is_started = condition == "started"
    scheduler = _Scheduler()
    if condition == "scheduled":
        monkeypatch.setattr(scheduler, "get_job", lambda _id: object())
    monkeypatch.setattr(
        account_crud_routes, "get_running_execution_id", lambda _id: "run" if condition == "running" else None
    )
    monkeypatch.setattr(
        account_crud_routes, "get_queued_execution_id", lambda _id: "queue" if condition == "queued" else None
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            account_crud_routes.copy_account(
                _RouteSession(source), scheduler, 1, account_crud_routes.AccountCopyRequest(name="新账户"), Response()
            )
        )
    assert exc.value.status_code == 409


def _build_account(
    *,
    preset: str = "default",
    trade_channel: TradeChannel = TradeChannel.CTP,
    override: dict[str, object] | None = None,
) -> Account:
    return Account(
        id=1,
        name="ctp-testnet-sim",
        market="期货",
        trade_channel=trade_channel,
        account_config={
            "broker_id": "9999",
            "investor_id": "test",
            "password": "test",
            "td_front": "tcp://td:1",
            "md_front": "tcp://md:2",
            "app_id": "app",
            "auth_code": "auth",
        },
        is_started=True,
        cron_expr="*/5 * * * *",
        remark=None,
        brokerage="ctp",
        weight_precision=0.001,
        long_leverage=1.0,
        short_leverage=1.0,
        algorithm={"method": "SINGLE-MAKER", "params": {}},
        empty_positions_algorithm=None,
        trade_rules={},
        forbidden_symbols=[],
        risk_symbols=[],
        feishu_key=None,
        portfolio_id=1,
        write_empty_record=0,
        account_control_preset=preset,
        account_control_override=override,
    )


def test_account_public_returns_connection_credentials_but_omits_webhook() -> None:
    account = _build_account()
    account.account_config["password"] = "unique-account-password"
    account.feishu_key = "unique-feishu-webhook-key"

    payload = account_crud_routes._account_public(account).model_dump_json()

    assert "unique-account-password" in payload
    assert "unique-feishu-webhook-key" not in payload
    assert '"account_config"' not in payload
    assert '"feishu_key"' not in payload
    assert '"account_configured":true' in payload
    assert '"feishu_configured":true' in payload


def _account_payload() -> dict[str, object]:
    return {
        "name": "ctp-testnet-sim",
        "market": "期货",
        "trade_channel": "ctp",
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
        "account_control_preset": "default",
        "account_control_override": {
            "timezone": "Asia/Shanghai",
            "operations": {
                "place_order": {
                    "account": {
                        "per_minute": {"limit": 3, "on_trigger": "wait"},
                        "per_day": {"limit": 30, "on_trigger": "block"},
                        "min_interval_ms": {"limit": 300, "on_trigger": "wait"},
                    }
                }
            },
        },
    }


def _build_app(session: _RouteSession) -> FastAPI:
    app = FastAPI()
    app.include_router(account_routes.router)

    async def _override_get_db() -> AsyncGenerator[_RouteSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_scheduler] = lambda: _Scheduler()
    return app


async def _noop_async(*_args: object, **_kwargs: object) -> None:
    return None


async def _synchronized_runtime_sync(*_args: object, **_kwargs: object) -> AccountRuntimeSync:
    """隔离账户 PATCH 契约测试，不让运行态同步影响 DTO 断言。"""
    return AccountRuntimeSync(account_id=1, status="synchronized")


def test_create_account_requires_explicit_account_control_preset() -> None:
    """创建账户时必须显式提供 account_control_preset。"""
    session = _RouteSession()
    payload = _account_payload()
    payload.pop("account_control_preset")

    response = TestClient(_build_app(session)).post("/account/", json=payload)

    assert response.status_code == 422
    assert any(error["loc"][-1] == "account_control_preset" for error in response.json()["detail"])


def test_create_account_rejects_unknown_account_control_preset(monkeypatch) -> None:
    """创建账户时未知 preset 应返回 422。"""
    monkeypatch.setattr(account_crud_routes, "parse_cron_expr", lambda _expr: ["fake-trigger"])
    monkeypatch.setattr(account_crud_routes, "add_record_portfolio_account", _noop_async)
    session = _RouteSession()
    payload = _account_payload()
    payload["account_control_preset"] = "missing"

    response = TestClient(_build_app(session)).post("/account/", json=payload)

    assert response.status_code == 422
    assert "未知的账户控制 preset" in response.json()["detail"]
    assert session.account is None


def test_create_account_rejects_incompatible_account_control_preset(monkeypatch) -> None:
    """创建账户时 preset 与渠道不兼容应返回 422。"""
    monkeypatch.setattr(account_crud_routes, "parse_cron_expr", lambda _expr: ["fake-trigger"])
    monkeypatch.setattr(account_crud_routes, "add_record_portfolio_account", _noop_async)
    session = _RouteSession()
    payload = _account_payload()
    payload["trade_channel"] = "gm"
    payload["account_config"] = {
        "account_id": "gm-account",
        "token": "token",
        "serv_addr": "127.0.0.1:7001",
    }
    payload["account_control_preset"] = "ctp"

    response = TestClient(_build_app(session)).post("/account/", json=payload)

    assert response.status_code == 422
    assert "不兼容" in response.json()["detail"]
    assert session.account is None


def test_create_account_rejects_invalid_channel_config_with_field_location() -> None:
    """创建接口应在落库前按渠道模型校验，并返回稳定字段位置。"""
    session = _RouteSession()
    payload = _account_payload()
    account_config = payload["account_config"]
    assert isinstance(account_config, dict)
    account_config["td_front"] = "tcp://host"

    response = TestClient(_build_app(session)).post("/account/", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"].startswith("account_config.td_front:")
    assert session.account is None


def test_create_account_rejects_unknown_channel_config_field() -> None:
    """渠道模型未声明的字段不能进入账户配置。"""
    session = _RouteSession()
    payload = _account_payload()
    account_config = payload["account_config"]
    assert isinstance(account_config, dict)
    account_config["legacy_option"] = True

    response = TestClient(_build_app(session)).post("/account/", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"].startswith("account_config.legacy_option:")
    assert session.account is None


def test_create_account_persists_normalized_channel_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """创建账户只持久化当前模式字段，并写入渠道模型默认值。"""
    monkeypatch.setattr(account_crud_routes, "parse_cron_expr", lambda _expr: ["fake-trigger"])
    monkeypatch.setattr(account_crud_routes, "add_record_portfolio_account", _noop_async)
    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", _noop_async)
    monkeypatch.setattr(account_crud_routes, "reconcile_account_runtime", _synchronized_runtime_sync)
    session = _RouteSession()
    payload = _account_payload()
    payload["trade_channel"] = "tq"
    payload["brokerage"] = "tq"
    payload["account_config"] = {
        "channel_type": "ctp",
        "account_mode": "sim",
        "tq_username": "user",
        "tq_password": "secret",
        "broker_name": "hidden-broker",
        "account_id": "hidden-account",
        "account_password": "hidden-password",
    }

    response = TestClient(_build_app(session)).post("/account/", json=payload)

    assert response.status_code == 201
    assert "account_config" not in response.json()
    assert session.account is not None
    assert session.account.account_config == {
        "account_mode": "sim",
        "tq_username": "user",
        "tq_password": "secret",
        "initial_balance": 10_000_000.0,
    }


def test_update_account_persists_normalized_channel_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """更新账户与创建使用同一规范化落库路径。"""
    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", _noop_async)
    monkeypatch.setattr(account_crud_routes, "reconcile_account_runtime", _synchronized_runtime_sync)
    session = _RouteSession(_build_account())

    response = TestClient(_build_app(session)).patch(
        "/account/1",
        json={
            "trade_channel": "tq",
            "account_config": {
                "channel_type": "gm",
                "account_mode": "kq",
                "tq_username": "user",
                "tq_password": "secret",
                "broker_name": "hidden-broker",
                "account_id": "hidden-account",
                "account_password": "hidden-password",
                "initial_balance": 12345,
            },
        },
    )

    assert response.status_code == 200
    assert "account_config" not in response.json()
    assert session.account is not None
    assert session.account.account_config == {"account_mode": "kq", "tq_username": "user", "tq_password": "secret"}


def test_update_account_can_switch_account_control_preset(monkeypatch) -> None:
    """CTP 账户应允许显式切换到 ctp preset。"""
    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", _noop_async)
    monkeypatch.setattr(account_crud_routes, "reconcile_account_runtime", _synchronized_runtime_sync)
    session = _RouteSession(_build_account(trade_channel=TradeChannel.CTP))

    response = TestClient(_build_app(session)).patch(
        "/account/1",
        json={"account_control_preset": "ctp"},
    )

    assert response.status_code == 200
    assert response.json()["account_control_preset"] == "ctp"
    assert session.account is not None
    assert session.account.account_control_preset == "ctp"


def test_update_account_replaces_account_control_override_instead_of_merging(monkeypatch) -> None:
    """PATCH 中 account_control_override 应整字段替换，而不是深度合并。"""
    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", _noop_async)
    monkeypatch.setattr(account_crud_routes, "reconcile_account_runtime", _synchronized_runtime_sync)
    session = _RouteSession(
        _build_account(
            override={
                "timezone": "Asia/Shanghai",
                "operations": {
                    "place_order": {
                        "account": {
                            "per_minute": {"limit": 3, "on_trigger": "wait"},
                            "per_day": {"limit": 30, "on_trigger": "block"},
                            "min_interval_ms": {"limit": 300, "on_trigger": "wait"},
                        }
                    }
                },
            }
        )
    )

    response = TestClient(_build_app(session)).patch(
        "/account/1",
        json={
            "account_control_override": {
                "operations": {
                    "cancel_order": {
                        "account": {
                            "per_day": {"limit": 8, "on_trigger": "block"},
                            "min_interval_ms": {"limit": 900, "on_trigger": "wait"},
                        }
                    }
                },
            }
        },
    )

    assert response.status_code == 200
    override = response.json()["account_control_override"]
    assert override["timezone"] is None
    assert set(override["operations"]) == {"cancel_order"}
    assert override["operations"]["cancel_order"]["account"]["per_day"] == {
        "limit": 8,
        "on_trigger": "block",
        "unlimited": False,
    }
    assert override["operations"]["cancel_order"]["account"]["min_interval_ms"] == {
        "limit": 900,
        "on_trigger": "wait",
        "unlimited": False,
    }
    assert override["operations"]["cancel_order"]["symbol"] is None
    assert session.account is not None
    assert session.account.account_control_override.timezone is None
    assert session.account.account_control_override.operations == {
        "cancel_order": session.account.account_control_override.operations["cancel_order"]
    }
    assert session.account.account_control_override.operations["cancel_order"].account.per_day.limit == 8
    assert session.account.account_control_override.operations["cancel_order"].account.min_interval_ms.limit == 900


def test_update_account_accepts_null_account_control_override_to_clear(monkeypatch) -> None:
    """PATCH 传 null 应明确清空 override。"""
    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", _noop_async)
    monkeypatch.setattr(account_crud_routes, "reconcile_account_runtime", _synchronized_runtime_sync)
    session = _RouteSession(
        _build_account(
            override={
                "timezone": "Asia/Shanghai",
                "operations": {
                    "place_order": {
                        "account": {
                            "per_minute": {"limit": 3, "on_trigger": "wait"},
                            "min_interval_ms": {"limit": 300, "on_trigger": "wait"},
                        },
                    }
                },
            }
        )
    )

    response = TestClient(_build_app(session)).patch(
        "/account/1",
        json={"account_control_override": None},
    )

    assert response.status_code == 200
    assert response.json()["account_control_override"] is None
    assert session.account is not None
    assert session.account.account_control_override is None


def test_update_account_rejects_removed_insert_order_override(monkeypatch) -> None:
    """已移除的 insert_order 不能再作为账户流控操作提交。"""
    monkeypatch.setattr(account_routes, "_reconcile_account_job", _noop_async)
    session = _RouteSession(_build_account(trade_channel=TradeChannel.CTP))

    response = TestClient(_build_app(session)).patch(
        "/account/1",
        json={
            "account_control_override": {
                "operations": {
                    "insert_order": {
                        "account": {"per_minute": {"limit": 3, "on_trigger": "wait"}},
                    }
                }
            }
        },
    )

    assert response.status_code == 422
    assert "未注册的 operation key" in str(response.json()["detail"])


def test_update_account_persists_and_clears_risk_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    """风险品种是 PATCH 的显式字段，列表与 null 清空语义都应落库并回显。"""
    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", _noop_async)
    monkeypatch.setattr(account_crud_routes, "reconcile_account_runtime", _synchronized_runtime_sync)
    session = _RouteSession(_build_account())
    client = TestClient(_build_app(session))

    updated = client.patch("/account/1", json={"risk_symbols": ["rb2610"]})

    assert updated.status_code == 200
    assert updated.json()["risk_symbols"] == ["rb2610"]
    assert session.account is not None
    assert session.account.risk_symbols == ["rb2610"]

    cleared = client.patch("/account/1", json={"risk_symbols": None})

    assert cleared.status_code == 200
    assert cleared.json()["risk_symbols"] is None
    assert session.account.risk_symbols is None


@pytest.mark.parametrize("weight_precision", [0, -0.01, 0.03])
def test_update_account_rejects_invalid_weight_precision_without_mutation(weight_precision: float) -> None:
    """创建和更新都必须在请求边界拒绝非法精度，不能进入除法路径。"""
    session = _RouteSession(_build_account())

    response = TestClient(_build_app(session)).patch("/account/1", json={"weight_precision": weight_precision})

    assert response.status_code == 422
    assert session.account is not None
    assert session.account.weight_precision == 0.001

    create_session = _RouteSession()
    create_payload = _account_payload()
    create_payload["weight_precision"] = weight_precision
    create_response = TestClient(_build_app(create_session)).post("/account/", json=create_payload)
    assert create_response.status_code == 422
    assert create_session.account is None


def test_update_same_channel_preserves_omitted_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """同渠道只改连接地址时，原凭证仍保留。"""
    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", _noop_async)
    monkeypatch.setattr(account_crud_routes, "reconcile_account_runtime", _synchronized_runtime_sync)
    session = _RouteSession(_build_account())
    response = TestClient(_build_app(session)).patch("/account/1", json={"account_config": {"td_front": "tcp://new:1"}})
    assert response.status_code == 200
    assert session.account is not None
    assert session.account.account_config["td_front"] == "tcp://new:1"
    assert session.account.account_config["password"] == "test"


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
def test_update_gm_keeps_session_until_connection_changes(monkeypatch, patch, reset_expected) -> None:
    """调度开关和重复保存不能销毁可复用的 GM 登录会话。"""
    account = _build_account(trade_channel=TradeChannel.GM)
    account.account_config = {
        "connection_mode": "service",
        "account_id": "test-account",
        "token": "test-token",
        "serv_addr": "127.0.0.1:7001",
    }
    requested_resets = []

    async def enqueue(_session, _account_id, *, reset_worker):
        requested_resets.append(reset_worker)

    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", enqueue)
    monkeypatch.setattr(account_crud_routes, "reconcile_account_runtime", _synchronized_runtime_sync)
    session = _RouteSession(account)

    response = TestClient(_build_app(session)).patch("/account/1", json=patch)

    assert response.status_code == 200, response.text
    assert requested_resets == [reset_expected]
    assert response.json()["is_started"] == patch.get("is_started", True)


def test_copy_account_database_history_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """有历史的账户复制后，新账户的执行、资产和收益独立开始。"""

    async def no_sync(*args, **kwargs):
        return None

    async def reconciled(*args, **kwargs):
        return SimpleNamespace(status="synchronized")

    monkeypatch.setattr(account_crud_routes, "enqueue_account_runtime_sync", no_sync)
    monkeypatch.setattr(account_crud_routes, "_reconcile_committed_runtime", reconciled)
    monkeypatch.setattr(account_crud_routes, "_account_public_with_runtime_sync", lambda account, sync: account)
    monkeypatch.setattr(account_crud_routes, "get_running_execution_id", lambda _: None)
    monkeypatch.setattr(account_crud_routes, "get_queued_execution_id", lambda _: None)

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                source = _build_account()
                source.is_started = False
                session.add(
                    Portfolio(
                        id=1,
                        name="组合",
                        market="期货",
                        custom_calc_py_code="def calculate_portfolio(context): return {}",
                    )
                )
                session.add(source)
                await session.flush()
                old_result = {"account_assets": {"total_asset": 100}, "orders": [{"order_id": "old-order"}]}
                session.add(
                    ExecuteRecord(
                        account_id=1,
                        execution_id="old",
                        is_success=1,
                        raw_result=old_result,
                        created_at="2026-01-01T09:00:00",
                    )
                )
                session.add(
                    AccountAssetSnapshot(
                        account_id=1, source="execution", execution_id="old", assets={"total_asset": 100}
                    )
                )
                session.add(
                    ExecutionEvent(
                        account_id=1,
                        execution_id="old",
                        channel=source.trade_channel,
                        algorithm="SINGLE-MAKER",
                        event_uid="old-event",
                        event_type=ExecutionEventType.ORDER_SUBMITTED,
                        status=ExecutionEventStatus.INFO,
                        reason_family=ExecutionReasonFamily.SYSTEM,
                        reason_code="ORDER.SUBMITTED",
                        order_id="old-order",
                    )
                )
                session.add(PortfolioAccount(account_id=1, portfolio_id=None))
                session.add(PortfolioAccount(account_id=1, portfolio_id=1))
                await session.commit()
                copied = await account_crud_routes.copy_account(
                    session, _Scheduler(), 1, account_crud_routes.AccountCopyRequest(name="新账户"), Response()
                )
                copied_id = copied.id
                assert copied_id != 1
                assert copied.is_started is False
                for field in AccountCreate.model_fields:
                    if field not in {"name", "is_started", "account_config"}:
                        assert getattr(copied, field) == getattr(source, field)
                assert all(copied.account_config[key] == value for key, value in source.account_config.items())
                history = await account_crud_routes.list_execute_records(session, copied_id, HistoryPagination())
                assert history.count == 0
                assert history.data == []
                assert (
                    await session.execute(select(ExecutionEvent).where(ExecutionEvent.account_id == copied_id))
                ).scalars().all() == []
                assert (
                    await session.execute(
                        select(AccountAssetSnapshot).where(AccountAssetSnapshot.account_id == copied_id)
                    )
                ).scalars().all() == []
                bindings = (
                    (await session.execute(select(PortfolioAccount).where(PortfolioAccount.account_id == copied_id)))
                    .scalars()
                    .all()
                )
                assert len(bindings) == 1
                assert bindings[0].portfolio_id == 1
                empty = await _account_performance(session, copied_id, "all")
                assert empty.baseline is None
                assert empty.points == []
                session.add(
                    ExecuteRecord(
                        account_id=copied_id,
                        execution_id="new",
                        is_success=1,
                        raw_result={"account_assets": {"total_asset": 250}},
                        created_at="2026-02-01T09:00:00",
                    )
                )
                await session.commit()
                history = await account_crud_routes.list_execute_records(session, copied_id, HistoryPagination())
                assert history.count == 1
                assert history.data[0].execution_id == "new"
                original = await account_crud_routes.list_execute_records(session, 1, HistoryPagination())
                assert original.count == 1
                assert original.data[0].raw_result["orders"] == old_result["orders"]
                stored = (
                    await session.execute(select(ExecuteRecord).where(ExecuteRecord.account_id == 1))
                ).scalar_one()
                assert stored.raw_result == old_result
                events = (
                    (await session.execute(select(ExecutionEvent).where(ExecutionEvent.account_id == 1)))
                    .scalars()
                    .all()
                )
                assert len(events) == 1
                assert events[0].order_id == "old-order"
                assert (
                    len(
                        (
                            await session.execute(
                                select(AccountAssetSnapshot).where(AccountAssetSnapshot.account_id == 1)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    == 1
                )
                assert (
                    len(
                        (await session.execute(select(PortfolioAccount).where(PortfolioAccount.account_id == 1)))
                        .scalars()
                        .all()
                    )
                    == 2
                )
                performance = await _account_performance(session, copied_id, "all")
                assert performance.baseline == "2026-02-01T09:00:00"
                assert performance.points[0].account_return == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
