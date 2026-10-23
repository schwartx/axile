"""结构化执行事实用于通知预览与补发的读取边界。"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axile.server._native import _core
from axile.server.api.routes import account_feishu
from axile.server.execution.notification_records import build_persisted_notification_context
from tests.unit.server._execution_test_support import build_account


@pytest.fixture(autouse=True)
def preview_native_boundary(monkeypatch):
    """单元测试隔离原生边界；文件数据库集成另行覆盖真实 Store。"""
    monkeypatch.setattr(
        account_feishu, "native_operation", AsyncMock(side_effect=AssertionError("应显式提供预览原生回执"))
    )


def _record() -> SimpleNamespace:
    """不提供已删除的原始 JSON 属性。"""
    return SimpleNamespace(id=1, execution_id="execution-facts", created_at="2026-10-20T09:00:00")


def _saved_context() -> dict:
    return {
        "execution": {
            "status": "PARTIAL",
            "success": False,
            "error": "部分成交",
            "execution_time": 0.0,
            "supplements": [{"is_last": True}],
        },
        "assets": {"total_assets": 0.0, "market_value": None},
        "positions": None,
        "orders": [],
        "trades": [],
    }


def test_preview_prefers_outbox_without_loading_execution_detail(monkeypatch) -> None:
    """已保存的通知上下文保留补发事实，预览只修改副本。"""
    saved = _saved_context()
    original = deepcopy(saved)
    assets = AsyncMock(side_effect=AssertionError("预览不得查询渠道"))
    native = AsyncMock(return_value={"record": vars(_record()), "context": saved})
    monkeypatch.setattr(account_feishu, "native_operation", native)
    monkeypatch.setattr(account_feishu, "query_account_assets", assets)

    context = asyncio.run(account_feishu._load_notification_preview_context(build_account()))

    assert saved == original
    assert context["execution"]["supplements"] == [{"is_last": True}]
    assert context["execution"]["id"] == "execution-facts"
    assert context["execution"]["is_test"] is True
    assert context["assets"] == {"total_assets": 0.0, "market_value": None}
    assert context["positions"] is None
    native.assert_awaited_once_with("notifications_preview", {"account_id": 1})
    assets.assert_not_awaited()


def test_preview_reads_captured_context_from_business_facts(monkeypatch) -> None:
    """缺少 outbox 时使用结构化扩展中的捕获上下文，不制造资产。"""
    captured = _saved_context()
    original = deepcopy(captured)
    record = _record()
    detail = {"input": {}, "result": {"extra": {"server_notification_context": captured}}}
    assets = AsyncMock(side_effect=AssertionError("预览不得查询渠道"))
    monkeypatch.setattr(
        account_feishu, "native_operation", AsyncMock(return_value={"record": vars(record), "detail": detail})
    )
    monkeypatch.setattr(account_feishu, "query_account_assets", assets)

    context = asyncio.run(account_feishu._load_notification_preview_context(build_account()))

    assert captured == original
    assert list(context)[: len(original)] == list(original)
    assert list(context["execution"])[: len(original["execution"])] == list(original["execution"])
    assert list(context["assets"]) == list(original["assets"])
    assert context["positions"] is None
    assert context["execution"]["execution_time"] == 0.0
    assert context["execution"]["error"] == "部分成交"
    assert context["execution"]["notified_at"] == record.created_at
    assets.assert_not_awaited()


def test_preview_converts_native_error_to_value_error(monkeypatch) -> None:
    """原生事实校验失败沿用预览可展示的错误边界。"""
    error = _core.NativeError("INVALID_DATA", "execution facts are not complete")
    monkeypatch.setattr(account_feishu, "native_operation", AsyncMock(side_effect=error))

    with pytest.raises(ValueError, match="^execution facts are not complete$") as caught:
        asyncio.run(account_feishu._load_notification_preview_context(build_account()))

    assert caught.value.__cause__ is error


def test_preview_rejects_incomplete_canonical_output_without_channel_query(monkeypatch) -> None:
    """不完整事实给出明确错误，不连接渠道填补数据。"""
    assets = AsyncMock(side_effect=AssertionError("预览不得查询渠道"))
    monkeypatch.setattr(
        account_feishu,
        "native_operation",
        AsyncMock(return_value={"record": vars(_record()), "detail": {"input": {}, "result": {}}}),
    )
    monkeypatch.setattr(account_feishu, "query_account_assets", assets)

    with pytest.raises(ValueError, match="数据不完整"):
        asyncio.run(account_feishu._load_notification_preview_context(build_account()))

    assets.assert_not_awaited()


@pytest.mark.parametrize("explicit_null", [False, True])
@pytest.mark.parametrize("legacy_outcome", [False, True])
def test_preview_rebuild_preserves_explicit_null_input(monkeypatch, explicit_null, legacy_outcome) -> None:
    """缺省结果输入可以引用独立输入，显式空值不替换为另一份输入。"""
    account = build_account()
    inputs = account_feishu._test_input(account, curr_target={"rb2610": 0.0}, last_target={}).to_dict()
    inputs.pop("account_config")
    result = {
        "account_assets": {
            "available_cash": 0.0,
            "total_asset": 0.0,
            "market_value": None,
            "positions": [],
            "source": "real",
        },
        "status": "NOOP",
        "channel_type": str(account.trade_channel),
        "execution_time": 0.0,
    }
    if explicit_null:
        result["inputs"] = None
    if legacy_outcome:
        result.pop("status")
        result["outcome"] = "completed"
    monkeypatch.setattr(
        account_feishu,
        "native_operation",
        AsyncMock(return_value={"record": vars(_record()), "detail": {"input": inputs, "result": result}}),
    )

    context = asyncio.run(account_feishu._load_notification_preview_context(account))

    assert context["targets"]["current"] == ({} if explicit_null else {"rb2610": 0.0})
    assert context["assets"]["total_assets"] == 0.0
    assert context["assets"]["market_value"] is None
    assert context["positions"] == []


@pytest.mark.parametrize(
    "time_fields", [{}, {"update_time": None}, {"update_time": "2026-10-20T09:00:02.123456+08:00"}]
)
def test_rebuild_keeps_explicit_errors_and_native_order_time(time_fields) -> None:
    """展示兼容只补缺失字段，不覆盖技术错误，也不把历史订单时间改成现在。"""
    error = "RuntimeError: native callback failed\n  at channel.confirm(order_id='native-01')"
    payload = {
        "status": "PARTIAL",
        "error": error,
        "outcome_reason": "one contract remains",
        "channel_type": "ctp",
        "inputs": None,
        "account_assets": {
            "available_cash": 0.0,
            "total_asset": 100000.0,
            "market_value": None,
            "positions": [],
            "source": "real",
        },
        "symbol_results": {
            "rb2610": {
                "symbol": "rb2610",
                "algorithm": "SINGLE-MAKER",
                "status": "PARTIAL",
                "error": error,
                "orders": [
                    {
                        "order_id": "native-01",
                        "symbol": "rb2610",
                        "direction": "BUY",
                        "order_type": "LIMIT",
                        "status": "部分成交",
                        "volume": 2.0,
                        "price": 3500.0,
                        "create_time": "2026-10-20T09:00:01.000001+08:00",
                        **time_fields,
                    }
                ],
            }
        },
    }
    original = deepcopy(payload)

    context = build_persisted_notification_context(account_feishu._TestNotificationSource("ctp-sim"), payload, None, {})

    assert context["execution"]["error"] == error
    assert context["symbols"][0]["error"] == error
    assert ("update_time" in context["orders"][0]) == ("update_time" in time_fields)
    if "update_time" in time_fields:
        assert context["orders"][0]["update_time"] == time_fields["update_time"]
    assert payload == original
