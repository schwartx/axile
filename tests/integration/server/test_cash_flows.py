"""出入金的事务、重复同步、修订与真实分析读取验收。"""

import json

import pytest

from tests.conftest import native_store_call, require_native_store
from tests.integration.server.test_incremental_analysis import append, update
from tests.unit.server._execution_test_support import build_account
from tests.unit.server._runtime_db_support import runtime_rows

NOW = "2026-11-13T12:00:00+08:00"


@pytest.fixture
def account(native_database):
    """使用已有建库与账户事实辅助函数。"""
    native_store_call(
        native_database,
        "account_create",
        {
            "account": build_account(trade_channel="tq", is_started=False, portfolio_id=None).model_dump(mode="json"),
            "now": NOW,
        },
    )
    return native_database


def call(path, op, **body):
    """调用当前工作树真实原生扩展。"""
    return native_store_call(path, "cash_flow_operation", {"account_id": 1, "op": op, "now": NOW, **body})


def flow(**body):
    """构造发生在两次估值之间的入金。"""
    return {"direction": "deposit", "amount": 50, "occurred_at": "2026-01-02T09:00:00", **body}


def chart(path):
    """读取已提交的分析版本。"""
    status = native_store_call(path, "analysis_query", {"command": "status", "account_id": 1})
    return native_store_call(
        path, "analysis_query", {"command": "chart", "account_id": 1, "version": status["version"], "range": "all"}
    )


def test_cash_flow_crud_audit_and_atomic_sync(account):
    created = call(account, "create", **flow())
    edited = call(account, "edit", id=created["id"], revision=1, **flow(amount=60))
    assert edited["amount"] == 60
    with pytest.raises(require_native_store().NativeError, match="CASH_FLOW_CHANGED"):
        call(account, "edit", id=created["id"], revision=1, **flow())
    call(account, "void", id=created["id"], revision=2)
    assert call(account, "list")["data"][0]["status"] == "void"
    assert len(runtime_rows(account, "SELECT * FROM account_cash_flow_revision")) == 2
    with pytest.raises(require_native_store().NativeError):
        call(account, "sync", rows=[flow(external_id="ok"), flow(external_id="bad", amount=-1)])
    assert call(account, "list")["total"] == 1


def test_repeat_sync_linking_and_void_tombstone(account):
    manual = call(account, "create", **flow())
    assert call(account, "sync", rows=[flow(external_id="ctp:1")])["changed"] == 1
    assert call(account, "sync", rows=[flow(external_id="ctp:1")])["changed"] == 0
    channel = next(r for r in call(account, "list")["data"] if r["source"] == "channel")
    assert channel["status"] == "pending"
    assert channel["manual_candidates"][0]["id"] == manual["id"]
    call(account, "link", id=channel["id"], revision=1, manual_id=manual["id"], manual_revision=1)
    rows = call(account, "list")["data"]
    assert sum(r["amount"] for r in rows if r["status"] == "active") == 50
    call(account, "void", id=channel["id"], revision=2)
    assert call(account, "sync", rows=[flow(external_id="ctp:1")])["changed"] == 0
    assert all(r["status"] == "void" for r in call(account, "list")["data"])


def test_channel_reversal_and_manual_after_channel_cannot_double_count(account):
    call(account, "sync", rows=[flow(external_id="ctp:reversal")])
    manual = call(account, "create", **flow())
    assert manual["status"] == "pending"
    call(account, "void", id=manual["id"], revision=1)
    assert call(account, "sync", rows=[flow(external_id="ctp:reversal", cancelled=True)])["changed"] == 1
    assert all(r["status"] == "void" for r in call(account, "list")["data"])


def test_account_isolation_and_input_validation(account):
    for patch in [{"amount": 0}, {"amount": float("inf")}, {"occurred_at": "2030-01-01"}, {"direction": "other"}]:
        with pytest.raises((require_native_store().NativeError, ValueError)):
            call(account, "create", **flow(**patch))
    native_store_call(
        account,
        "account_create",
        {
            "account": build_account(trade_channel="tq", is_started=False, portfolio_id=None).model_dump(mode="json"),
            "now": NOW,
        },
    )
    created = call(account, "create", **flow())
    with pytest.raises(require_native_store().NativeError, match="CASH_FLOW_NOT_FOUND"):
        call(account, "edit", account_id=2, id=created["id"], revision=1, **flow())


def test_late_deposit_rebuilds_curve_and_contribution_metadata(account):
    append(account, 1, price=100)
    append(account, 2, price=150)
    update(account)
    assert chart(account)["result"]["points"][-1]["account_return"] == pytest.approx(0.5)
    created = call(account, "create", **flow())
    assert native_store_call(account, "analysis_query", {"command": "status", "account_id": 1})["status"] != "ready"
    update(account)
    result = chart(account)["result"]
    assert result["points"][-1]["account_return"] == pytest.approx(0)
    assert result["points"][-1]["account_equity"] == 150
    assert result["cash_flow_adjustment"]["approximate"]
    assert sum(f["amount"] for f in result["cash_flow_adjustment"]["flows"]) == 50
    append(account, 3, price=165)
    update(account)
    assert chart(account)["result"]["points"][-1]["account_return"] == pytest.approx(0.1)
    summaries = require_native_store().account_dashboard_context(str(account), json.dumps({}))
    assert summaries is not None
    call(account, "void", id=created["id"], revision=1)
    update(account)
    assert chart(account)["result"]["points"][-1]["account_return"] == pytest.approx(0.65)


def test_downgrade_refuses_losing_cash_flow_history(account):
    call(account, "create", **flow())
    with pytest.raises(require_native_store().NativeError, match="降级会丢失"):
        native_store_call(account, "migration_downgrade", {"public": "0036"})
    assert native_store_call(account, "migration_current", {})["public"] == "0037"
    assert call(account, "list")["total"] == 1


def test_current_asset_daily_return_excludes_deposit_before_replay(account):
    append(account, 1, price=100)
    append(account, 2, price=110)
    update(account)
    native_store_call(
        account,
        "asset_snapshot_write",
        {
            "account_id": 1,
            "created_at": "2026-01-03T09:00:00",
            "account_assets": {"total_asset": 160, "positions": [], "currency": "CNY", "source": "real"},
        },
    )
    call(account, "create", **flow(occurred_at="2026-01-03T09:00:00"))
    context = native_store_call(account, "account_dashboard_context", {})
    assert context["inputs"]["1"]["live_daily_return"] == pytest.approx(0)


def test_empty_upgrade_downgrade_and_repeat(native_database):
    native_store_call(native_database, "migration_downgrade", {"public": "0036"})
    assert native_store_call(native_database, "migration_current", {})["public"] == "0036"
    for _ in range(2):
        assert native_store_call(native_database, "migration_upgrade", {})["public"] == "0037"


def test_edit_manual_into_existing_channel_amount_requires_confirmation(account):
    call(account, "sync", rows=[flow(external_id="ctp:edit")])
    manual = call(account, "create", **flow(amount=40))
    edited = call(account, "edit", id=manual["id"], revision=1, **flow())
    assert edited["status"] == "pending"
    rows = call(account, "list")["data"]
    assert sum(r["amount"] for r in rows if r["status"] == "active") == 50
