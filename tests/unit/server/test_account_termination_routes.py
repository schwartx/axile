"""账户执行终止接口的路由测试。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionKind, ExecutionTaskStatus, ExecutionTerminateMode
from axile.server.api.routes import account as account_routes
from axile.server.api.routes import account_execution as account_execution_routes
from axile.server.db.models import Account
from axile.server.execution.registry import ExecutionTaskState


def _build_account() -> Account:
    return Account(
        id=1,
        name="ctp-testnet-sim",
        market="期货",
        trade_channel=TradeChannel.CTP,
        account_config={"api_key": "k", "secret_key": "s", "is_testnet": True},
        is_started=True,
        cron_expr="3,6,9 * * * *",
        remark=None,
        brokerage="ctp",
        weight_precision=0.001,
        long_leverage=1.0,
        short_leverage=1.0,
        algorithm={"method": "SINGLE-MAKER", "params": {}},
        trade_rules={},
        forbidden_symbols=[],
        risk_symbols=[],
        feishu_key=None,
        portfolio_id=1,
        write_empty_record=0,
    )


def _build_app(account: Account | None, monkeypatch) -> FastAPI:
    from axile.server.execution import supplements

    async def load(account_id: int):
        if account is None or account.id != account_id:
            raise HTTPException(status_code=404, detail="账户不存在")
        return account.snapshot()

    async def cancel(_account_id: int, _reason: str):
        return False

    monkeypatch.setattr(account_execution_routes, "_get_account_or_404", load)
    monkeypatch.setattr(account_execution_routes, "get_queued_execution_id", lambda _: None)
    monkeypatch.setattr(supplements, "cancel_groups_locked", cancel)
    app = FastAPI()
    app.include_router(account_routes.router)
    return app


def test_terminate_account_execution_route_returns_conflict_without_running_execution(monkeypatch) -> None:
    """当账户当前没有活跃 execution 时，应返回 409。"""
    monkeypatch.setattr(account_execution_routes, "get_running_execution_id", lambda _account_id: None)

    client = TestClient(_build_app(_build_account(), monkeypatch))
    response = client.post("/account/1/terminate", json={"mode": "graceful"})

    assert response.status_code == 409
    assert response.json()["detail"] == "账户当前没有活跃执行"


def test_terminate_account_execution_route_returns_running_execution(monkeypatch) -> None:
    """账户级终止接口应转发到当前运行中的 execution。"""

    async def fake_get_execution_status(_execution_id: str) -> dict[str, object]:
        return {
            "execution_id": "exec-2",
            "account_id": 1,
            "execution_kind": ExecutionKind.CLEAR_POSITIONS,
            "status": ExecutionTaskStatus.RUNNING,
            "created_at": "2026-03-22T10:00:00",
        }

    async def fake_terminate_running_account_execution(
        account_id: int,
        *,
        reason: str | None,
        mode: ExecutionTerminateMode,
    ) -> object:
        assert account_id == 1
        assert reason == "manual stop"
        assert mode == ExecutionTerminateMode.CANCEL_PENDING
        return ExecutionTaskState(
            execution_id="exec-2",
            account_id=1,
            execution_kind=ExecutionKind.CLEAR_POSITIONS,
            status=ExecutionTaskStatus.TERMINATING,
            created_at="2026-03-22T10:00:00",
        )

    monkeypatch.setattr(account_execution_routes, "get_running_execution_id", lambda _account_id: "exec-2")
    monkeypatch.setattr(account_execution_routes, "get_execution_status", fake_get_execution_status)
    monkeypatch.setattr(
        account_execution_routes,
        "terminate_running_account_execution",
        fake_terminate_running_account_execution,
    )

    client = TestClient(_build_app(_build_account(), monkeypatch))
    response = client.post(
        "/account/1/terminate",
        json={"reason": "manual stop", "mode": "cancel_pending"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "message": "已受理终止请求",
        "account_id": 1,
        "execution_id": "exec-2",
        "status": "TERMINATING",
    }
