"""账户控制审计路由测试。"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.models import AccountControlDecision
from axile.server.account_control_audit import (
    AccountControlAuditEventPage,
    AccountControlAuditExecutionPage,
    AccountControlAuditSummaryRow,
)
from axile.server.api.deps import get_db, get_scheduler
from axile.server.api.routes import account as account_routes
from axile.server.api.routes import account_control_audit_routes
from axile.server.db.models import Account, AccountControlEvent


class _RouteSession:
    def __init__(self, account: Account | None = None) -> None:
        self.account = account

    async def get(self, _model: object, account_id: int) -> Account | None:
        if self.account is not None and self.account.id == account_id:
            return self.account
        return None


class _Scheduler:
    def get_job(self, _job_id: str) -> None:
        return None


def _build_account() -> Account:
    return Account(
        id=1,
        name="ctp-testnet-sim",
        market="期货",
        trade_channel=TradeChannel.CTP,
        account_config={"api_key": "k", "secret_key": "s", "is_testnet": True},
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
        account_control_preset="default",
        account_control_override=None,
    )


def _build_app(session: _RouteSession) -> FastAPI:
    app = FastAPI()
    app.include_router(account_routes.router)

    async def _override_get_db() -> AsyncGenerator[_RouteSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_scheduler] = lambda: _Scheduler()
    return app


def test_account_control_audit_summary_route_requires_control_date(monkeypatch) -> None:
    """账户日汇总路由必须显式提供 control_date。"""
    session = _RouteSession(_build_account())

    async def _fake_summary(*_args: object, **_kwargs: object) -> list[AccountControlAuditSummaryRow]:
        return []

    monkeypatch.setattr(
        account_control_audit_routes.account_control_audit, "query_account_control_audit_summary", _fake_summary
    )

    response = TestClient(_build_app(session)).get("/account/1/control/audit/summary")

    assert response.status_code == 422
    assert any(error["loc"][-1] == "control_date" for error in response.json()["detail"])


def test_account_control_audit_executions_route_requires_control_date_and_enforces_limit(monkeypatch) -> None:
    """execution 列表路由应强制 control_date 且 limit 超限返回 422。"""
    session = _RouteSession(_build_account())

    async def _fake_executions(*_args: object, **_kwargs: object) -> AccountControlAuditExecutionPage:
        return AccountControlAuditExecutionPage(data=[], count=0)

    monkeypatch.setattr(
        account_control_audit_routes.account_control_audit, "query_account_control_audit_executions", _fake_executions
    )
    client = TestClient(_build_app(session))

    missing_date = client.get("/account/1/control/audit/executions")
    over_limit = client.get(
        "/account/1/control/audit/executions",
        params={"control_date": "2026-03-22", "limit": 501},
    )

    assert missing_date.status_code == 422
    assert any(error["loc"][-1] == "control_date" for error in missing_date.json()["detail"])
    assert over_limit.status_code == 422


def test_account_control_execution_events_route_supports_filters_and_uses_control_event_model(
    monkeypatch,
) -> None:
    """execution 级事件流路由应暴露账户控制事件，而不是 execution audit 事件。"""
    session = _RouteSession(_build_account())

    async def _fake_events(
        _session: object,
        *,
        execution_id: str,
        symbol: str | None = None,
        operation: str | None = None,
        skip: int = 0,
        limit: int = 200,
    ) -> AccountControlAuditEventPage:
        assert execution_id == "exec-1"
        assert symbol == "rb2610"
        assert operation == "place_order"
        assert skip == 0
        assert limit == 2
        return AccountControlAuditEventPage(
            data=[
                AccountControlEvent(
                    id=11,
                    account_id=1,
                    control_date="2026-03-22",
                    execution_id="exec-1",
                    seq=1,
                    channel=TradeChannel.CTP,
                    operation="place_order",
                    symbol="rb2610",
                    metadata_={"order_id": "oid-1"},
                    decision=AccountControlDecision.ALLOWED,
                    counted=True,
                    outcome="submitted",
                    event_uid="event-1",
                    created_at="2026-03-22T09:31:00",
                    occurred_at_ms=1_763_226_660_123,
                )
            ],
            count=1,
        )

    monkeypatch.setattr(
        account_control_audit_routes.account_control_audit, "query_account_control_execution_events", _fake_events
    )
    client = TestClient(_build_app(session))

    response = client.get(
        "/account/executions/exec-1/control/events",
        params={
            "symbol": "rb2610",
            "operation": "place_order",
            "limit": 2,
        },
    )
    over_limit = client.get(
        "/account/executions/exec-1/control/events",
        params={"limit": 501},
    )

    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                "account_id": 1,
                "control_date": "2026-03-22",
                "execution_id": "exec-1",
                "seq": 1,
                "channel": "ctp",
                "operation": "place_order",
                "symbol": "rb2610",
                "metadata": {"order_id": "oid-1"},
                "decision": "allowed",
                "counted": True,
                "outcome": "submitted",
                "event_uid": "event-1",
                "created_at": "2026-03-22T09:31:00",
                "occurred_at_ms": 1_763_226_660_123,
                "id": 11,
            }
        ],
        "count": 1,
    }
    assert over_limit.status_code == 422
