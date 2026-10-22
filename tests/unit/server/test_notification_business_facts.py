"""结构化执行事实用于通知预览与补发的读取边界。"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from axile.domain.execution import ExecutionKind, ExecutionTaskStatus
from axile.server._native import _core
from axile.server.api.routes import account_feishu
from axile.server.execution import supplement_notifications
from axile.server.execution.notification_records import build_persisted_notification_context
from tests.unit.server._execution_test_support import build_account


@pytest.fixture(autouse=True)
def preview_native_boundary(monkeypatch):
    """单元测试隔离原生边界；文件数据库集成另行覆盖真实 Store。"""
    monkeypatch.setattr(
        account_feishu, "_core", SimpleNamespace(execution_business_detail=object(), NativeError=_core.NativeError)
    )
    monkeypatch.setattr(account_feishu, "analysis_store", lambda: "preview-store")


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


def _intent(**changes) -> SimpleNamespace:
    fields = {
        "execution_id": "execution-facts",
        "kind": ExecutionKind.REBALANCE,
        "trigger_source": "manual",
        "status": ExecutionTaskStatus.FAILED,
        "error": None,
        "finished_at": "2026-10-20T09:00:01",
        "algorithm": "market",
    }
    return SimpleNamespace(**(fields | changes))


def test_preview_prefers_outbox_without_loading_execution_detail(monkeypatch) -> None:
    """已保存的通知上下文保留补发事实，预览只修改副本。"""
    saved = _saved_context()
    original = deepcopy(saved)
    detail = AsyncMock(side_effect=AssertionError("已有通知不应装载执行明细"))
    assets = AsyncMock(side_effect=AssertionError("预览不得查询渠道"))
    monkeypatch.setattr(account_feishu, "get_recent_execute_records_by_account_id", AsyncMock(return_value=[_record()]))
    monkeypatch.setattr(account_feishu, "run_in_threadpool", detail)
    monkeypatch.setattr(account_feishu, "query_account_assets", assets)
    session = SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(context=saved)))

    context = asyncio.run(account_feishu._load_notification_preview_context(session, build_account()))

    assert saved == original
    assert context["execution"]["supplements"] == [{"is_last": True}]
    assert context["execution"]["id"] == "execution-facts"
    assert context["execution"]["is_test"] is True
    assert context["assets"] == {"total_assets": 0.0, "market_value": None}
    assert context["positions"] is None
    detail.assert_not_awaited()
    assets.assert_not_awaited()


def test_preview_reads_captured_context_from_business_facts(monkeypatch) -> None:
    """缺少 outbox 时使用结构化扩展中的捕获上下文，不制造资产。"""
    captured = _saved_context()
    original = deepcopy(captured)
    record = _record()
    detail = AsyncMock(return_value={"input": {}, "result": {"extra": {"server_notification_context": captured}}})
    assets = AsyncMock(side_effect=AssertionError("预览不得查询渠道"))
    monkeypatch.setattr(account_feishu, "get_recent_execute_records_by_account_id", AsyncMock(return_value=[record]))
    monkeypatch.setattr(account_feishu, "run_in_threadpool", detail)
    monkeypatch.setattr(account_feishu, "query_account_assets", assets)
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    context = asyncio.run(account_feishu._load_notification_preview_context(session, build_account()))

    assert captured == original
    assert list(context)[: len(original)] == list(original)
    assert list(context["execution"])[: len(original["execution"])] == list(original["execution"])
    assert list(context["assets"]) == list(original["assets"])
    assert context["positions"] is None
    assert context["execution"]["execution_time"] == 0.0
    assert context["execution"]["error"] == "部分成交"
    assert context["execution"]["notified_at"] == record.created_at
    detail.assert_awaited_once_with(
        account_feishu._core.execution_business_detail, account_feishu.analysis_store(), 1, record.id
    )
    assets.assert_not_awaited()


def test_preview_converts_native_error_to_value_error(monkeypatch) -> None:
    """原生事实校验失败沿用预览可展示的错误边界。"""
    error = _core.NativeError("INVALID_DATA", "execution facts are not complete")
    monkeypatch.setattr(account_feishu, "get_recent_execute_records_by_account_id", AsyncMock(return_value=[_record()]))
    monkeypatch.setattr(account_feishu, "run_in_threadpool", AsyncMock(side_effect=error))
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    with pytest.raises(ValueError, match="^execution facts are not complete$") as caught:
        asyncio.run(account_feishu._load_notification_preview_context(session, build_account()))

    assert caught.value.__cause__ is error


def test_preview_rejects_incomplete_canonical_output_without_channel_query(monkeypatch) -> None:
    """不完整事实给出明确错误，不连接渠道填补数据。"""
    assets = AsyncMock(side_effect=AssertionError("预览不得查询渠道"))
    monkeypatch.setattr(account_feishu, "get_recent_execute_records_by_account_id", AsyncMock(return_value=[_record()]))
    monkeypatch.setattr(account_feishu, "run_in_threadpool", AsyncMock(return_value={"input": {}, "result": {}}))
    monkeypatch.setattr(account_feishu, "query_account_assets", assets)
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    with pytest.raises(ValueError, match="数据不完整"):
        asyncio.run(account_feishu._load_notification_preview_context(session, build_account()))

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
    monkeypatch.setattr(account_feishu, "get_recent_execute_records_by_account_id", AsyncMock(return_value=[_record()]))
    monkeypatch.setattr(
        account_feishu, "run_in_threadpool", AsyncMock(return_value={"input": inputs, "result": result})
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    context = asyncio.run(account_feishu._load_notification_preview_context(session, account))

    assert context["targets"]["current"] == ({} if explicit_null else {"rb2610": 0.0})
    assert context["assets"]["total_assets"] == 0.0
    assert context["assets"]["market_value"] is None
    assert context["positions"] == []


def test_supplement_context_does_not_modify_captured_facts(monkeypatch) -> None:
    """终态与补发信息只写入新通知副本，保留零值、空集合与缺失资产。"""
    captured = _saved_context()
    original = deepcopy(captured)
    record = _record()
    detail = AsyncMock(return_value={"input": {}, "result": {"extra": {"server_notification_context": captured}}})
    monkeypatch.setattr(supplement_notifications, "read_execution_detail", detail)
    session = SimpleNamespace(scalar=AsyncMock(return_value=record))
    row = _intent(error="intent failure")

    context = asyncio.run(supplement_notifications._execution_context(session, row, build_account()))

    assert captured == original
    assert context["execution"]["status"] == "PARTIAL"
    assert context["execution"]["error"] == "intent failure"
    assert context["execution"]["execution_time"] == 0.0
    assert context["execution"]["is_test"] is False
    assert context["positions"] is None
    assert context["assets"] == {"total_assets": 0.0, "market_value": None}
    assert context["trades"] == []
    assert context["event_id"] == "execution:execution-facts"
    detail.assert_awaited_once_with(session, record)


def test_early_failure_context_keeps_unknown_assets_and_intent_error(monkeypatch) -> None:
    """执行器尚未产生事实时，失败通知不得把未知权益变成零。"""
    detail = AsyncMock(side_effect=AssertionError("没有执行记录"))
    monkeypatch.setattr(supplement_notifications, "read_execution_detail", detail)
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    row = _intent(error="input persistence failed")

    context = asyncio.run(supplement_notifications._execution_context(session, row, build_account()))

    assert context["execution"]["error"] == "input persistence failed"
    assert context["execution"]["status"] == "FAILED"
    assert context["assets"] == {}
    assert context["positions"] is None
    assert context["summary"]["position_count"] is None
    detail.assert_not_awaited()


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
