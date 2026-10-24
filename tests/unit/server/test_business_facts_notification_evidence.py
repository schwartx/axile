"""真实事实表到通知及证据归档的独立离线回归。"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
import sqlalchemy as sa

from axile.domain.execution import (
    ExecutionArtifactType,
    ExecutionEventStatus,
    ExecutionEventType,
    ExecutionKind,
    ExecutionReasonFamily,
    ExecutionTaskStatus,
)
from axile.executor import feishu_notifications
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.server._native import _core
from axile.server.api.routes import account_feishu
from axile.server.business_facts import repository as fact_repository
from axile.server.business_facts.artifacts import (
    BusinessFactReferenceError,
    make_artifact_reference,
    resolve_artifact_content,
)
from axile.server.business_facts.assets import (
    input_after_asset_fact_id,
    input_asset_fact_id,
)
from axile.server.business_facts.repository import read_execution_detail
from axile.server.db.models.execution import ExecutionArtifact, ExecutionEvent
from axile.server.db.models.supplement import SupplementGroup, SupplementStep
from axile.server.evidence.archive import build_archive
from axile.server.evidence.collect import collect_evidence
from axile.server.evidence.models import EvidenceError, EvidenceLimits, EvidenceProfile, ExportBudget
from axile.server.execution import records
from scripts.business_facts_contract import business_evidence
from tests.conftest import initialize_native_database, native_store_call
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server._business_facts_test_support import freeze_input, persist_result, rows, sql
from tests.unit.server._execution_test_support import build_account
from tests.unit.server.test_evidence_export import _seed

_EXECUTION = "facts-notification-evidence"
_CREATED = "2026-10-20T09:00:10+08:00"
_TECHNICAL_ERROR = "RuntimeError: native callback failed\n" + "  at channel.confirm(order_id='native-01')\n" * 128


@pytest.fixture
def facts_database(tmp_path, monkeypatch):
    """真实迁移初始化临时库，独立 SQL 连接在原生调用前关闭。"""
    path = tmp_path / "facts-evidence.db"
    initialize_native_database(path)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: str(path))
    _seed(path, build_account(portfolio_id=None))
    return path


def _get(path, name):
    values = rows(path, name)
    return SimpleNamespace(**values[0]) if values else None


async def _finish(path):
    return native_store_call(
        path,
        "intent_finish",
        {
            "execution_id": _EXECUTION,
            "status": "PARTIAL",
            "finished_at": _CREATED,
            "now": _CREATED,
        },
    )


def _input() -> dict:
    return {
        "channel_type": "ctp",
        "curr_target": {"rb2610": 0.25, "zero-target": 0},
        "last_target": {},
        "algorithm": {"method": "SINGLE-MAKER", "params": {}},
        "trade_rules": {},
        "feishu_account": {"id": 1, "name": "ctp-sim"},
        "extra": {"audit": {"execution_id": _EXECUTION, "execution_kind": "rebalance", "trigger_source": "manual"}},
    }


def _result() -> dict:
    """已知零、缺行情与原生成交值在同一份证据中共存。"""
    return {
        "status": "PARTIAL",
        "success": False,
        "error": _TECHNICAL_ERROR,
        "outcome": "not_reached",
        "outcome_reason": "one contract remains",
        "reason_code": None,
        "execution_time": 0.0,
        "channel_type": "ctp",
        "inputs": _input(),
        "account_assets": {
            "available_cash": 0.0,
            "total_asset": 100000.0,
            "market_value": None,
            "currency": "CNY",
            "update_time": "2026-10-20T09:00:09.123456+08:00",
            "source": "real",
            "positions": [
                {
                    "symbol": "rb2610",
                    "volume": 1.0,
                    "available_volume": 0.0,
                    "market_value": None,
                    "direction": "多头",
                    "avg_price": 3500.0,
                    "extra": {"position_attributes": {"today": 1, "yesterday": 0}},
                }
            ],
        },
        "symbol_results": {
            "rb2610": {
                "symbol": "rb2610",
                "algorithm": "SINGLE-MAKER",
                "status": "PARTIAL",
                "error": "one contract remains",
                "target_volume": 2,
                "final_volume": 1,
                "first_tick": None,
                "orders": [
                    {
                        "order_id": "native-01",
                        "symbol": "rb2610",
                        "direction": "BUY",
                        "order_type": "LIMIT",
                        "status": "部分成交",
                        "volume": 2,
                        "price": 3500.0,
                        "filled_volume": 1,
                        "avg_price": 3500.0,
                        "create_time": "2026-10-20T09:00:01.000001+08:00",
                        "update_time": "2026-10-20T09:00:02.000002+08:00",
                        "extra": {
                            "OrderSysID": "native-system-01",
                            "exchange": "SHFE",
                            "trading_day": "20261020",
                        },
                    }
                ],
                "trades": [
                    {
                        "trade_id": "native-fill-01",
                        "order_id": "native-01",
                        "symbol": "rb2610",
                        "trade_time": "2026-10-20T09:00:02.000003+08:00",
                        "trade_volume": 1,
                        "trade_price": 3500.0,
                        "trade_value": 35000.125,
                        "extra": {"commission": 0, "commission_asset": "CNY"},
                    }
                ],
                "memory": {},
            }
        },
        "memory": {},
        "extra": {},
    }


async def _store(path, *, result=None, event_context=None):
    payload = _result() if result is None else result
    record = persist_result(path, deepcopy(payload), execution_id=_EXECUTION, created_at=_CREATED, raw_input=_input())
    _seed(
        path,
        (
            "execution_intent",
            dict(
                account_id=1,
                execution_id=_EXECUTION,
                kind=ExecutionKind.REBALANCE,
                trigger_source="manual",
                status=ExecutionTaskStatus.RUNNING,
                channel="ctp",
                algorithm="SINGLE-MAKER",
                created_at="2026-10-20T09:00:00+08:00",
                started_at="2026-10-20T09:00:01+08:00",
            ),
        ),
    )
    if event_context is not None:
        _seed(
            path,
            (
                "notification_event",
                dict(
                    id=f"execution:{_EXECUTION}",
                    account_id=1,
                    event_type="execution.finished",
                    context=deepcopy(event_context),
                ),
            ),
        )
    return record["id"]


async def _collect(path, budget=None):
    return await collect_evidence(budget or ExportBudget(), execution_id=_EXECUTION)


def _csv(archive: ZipFile, path: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(archive.read(path).decode("utf-8-sig"))))


def _resolve_pointer(document: dict, pointer: str):
    """独立解释归档指针，检查每个来源确实指向已导出内容。"""
    current = document
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def _stable_context(context: dict) -> dict:
    """预览自带测试标记和历史通知时点，其余业务内容逐字段比较。"""
    result = deepcopy(context)
    result.pop("is_test", None)
    result["execution"].pop("is_test", None)
    result["execution"].pop("notified_at", None)
    return result


def test_roundtrip_preview_matches_prior_notification_semantics_without_channel(facts_database, monkeypatch):
    """新事实表重建与直接标准输出生成的业务通知内容一致。"""
    payload = _result()
    account = build_account(portfolio_id=None)
    old_payload = deepcopy(payload)
    old_payload["inputs"]["account_config"] = account.account_config
    original_order = old_payload["symbol_results"]["rb2610"]["orders"][0]
    original_order["extra"]["_update_time_str"] = original_order["update_time"]
    monkeypatch.setattr(feishu_notifications, "clock_now", lambda: datetime(2026, 10, 20, 1, tzinfo=UTC))
    monkeypatch.setattr(account_feishu, "clock_now", lambda: datetime(2026, 10, 20, 1, tzinfo=UTC))
    previous = feishu_notifications.build_execution_notification_context(
        account_feishu._TestNotificationSource(account.name), UnifiedStandardOutput.model_validate(old_payload)
    )
    asyncio.run(_store(facts_database, result=payload))
    channel = AsyncMock(side_effect=AssertionError("historical preview must not connect to a channel"))
    monkeypatch.setattr(account_feishu, "query_account_assets", channel)

    async def preview():
        return await account_feishu._load_notification_preview_context(account)

    context = asyncio.run(preview())

    assert business_evidence(_stable_context(context)) == business_evidence(_stable_context(previous))
    assert business_evidence(context["assets"]["available_cash"]) == business_evidence(0.0)
    assert context["positions"][0]["market_value"] is None
    assert business_evidence(context["trades"][0]["trade_value"]) == business_evidence(35000.125)
    assert business_evidence(context["orders"][0]["update_time"]) == business_evidence(
        payload["symbol_results"]["rb2610"]["orders"][0]["update_time"]
    )
    assert business_evidence(context["execution"]["error"]) == business_evidence(_TECHNICAL_ERROR)
    assert business_evidence(context["execution"]["notified_at"]) == business_evidence(_CREATED)
    channel.assert_not_awaited()


@pytest.mark.parametrize("context_source", ["captured", "outbox"])
def test_real_persisted_preview_context_is_deeply_detached(facts_database, context_source):
    """修改预览的嵌套字典、列表，不能污染捕获事实或 outbox。"""
    saved = {
        "execution": {"status": "PARTIAL", "error": _TECHNICAL_ERROR, "supplements": [{"is_last": True}]},
        "assets": {"available_cash": 0, "total_assets": None},
        "positions": None,
        "orders": [{"order_id": "native-01", "extra": {"marker": "preserved"}}],
        "trades": [],
    }
    payload = _result()
    if context_source == "captured":
        payload["extra"]["server_notification_context"] = deepcopy(saved)
    asyncio.run(_store(facts_database, result=payload, event_context=saved if context_source == "outbox" else None))

    async def inspect_preview():
        context = await account_feishu._load_notification_preview_context(build_account(portfolio_id=None))
        if context_source == "captured":
            record = _get(facts_database, "executerecord")
            native = await read_execution_detail(record)
            captured = native["result"]["extra"]["server_notification_context"]
            assert list(captured) == list(saved)
            assert list(captured["execution"]) == list(saved["execution"])
            assert list(captured["assets"]) == list(saved["assets"])
            assert list(captured["orders"][0]) == list(saved["orders"][0])
        context["orders"][0]["extra"]["marker"] = "changed"
        context["execution"]["supplements"][0]["is_last"] = False
        if context_source == "outbox":
            event = _get(facts_database, "notification_event")
            return event.context
        record = _get(facts_database, "executerecord")
        detail = await read_execution_detail(record)
        return detail["result"]["extra"]["server_notification_context"]

    assert business_evidence(asyncio.run(inspect_preview())) == business_evidence(saved)


def test_supplement_notification_keeps_full_error_after_facts_roundtrip(facts_database):
    """终态补发仍使用完整错误字符串并保留资产缺失语义。"""
    asyncio.run(_store(facts_database))

    async def context():
        return await account_feishu._load_notification_preview_context(build_account(portfolio_id=None))

    result = asyncio.run(context())

    assert business_evidence(result["execution"]["error"]) == business_evidence(_TECHNICAL_ERROR)
    assert business_evidence(result["execution"]["status"]) == business_evidence("PARTIAL")
    assert business_evidence(result["execution"]["execution_time"]) == business_evidence(0.0)
    assert result["assets"]["market_value"] is None
    assert business_evidence(result["summary"]["trade_count"]) == business_evidence(1)


def test_notification_uses_result_input_while_evidence_keeps_frozen_pretrade_input(facts_database, monkeypatch):
    """两份历史输入不同，各自的语义不能被共享引用或重建覆盖。"""
    payload = _result()
    payload["inputs"]["curr_target"] = {"rb2610": 0.75}
    asyncio.run(_store(facts_database, result=payload))
    channel = AsyncMock(side_effect=AssertionError("historical preview must not connect to a channel"))
    monkeypatch.setattr(account_feishu, "query_account_assets", channel)

    async def preview():
        return await account_feishu._load_notification_preview_context(build_account(portfolio_id=None))

    context = asyncio.run(preview())
    item = asyncio.run(_collect(facts_database)).executions[_EXECUTION]

    assert business_evidence(context["targets"]["current"]) == business_evidence({"rb2610": 0.75})
    assert business_evidence(item.record["input"]["curr_target"]) == business_evidence(
        {"rb2610": 0.25, "zero-target": 0}
    )
    assert business_evidence(item.record["result"]["inputs"]["curr_target"]) == business_evidence({"rb2610": 0.75})
    channel.assert_not_awaited()


@pytest.mark.parametrize("input_state", ["absent", "null"])
def test_notification_distinguishes_omitted_and_explicit_null_result_input(facts_database, monkeypatch, input_state):
    """缺少结果输入时可取冻结输入；显式空值不能被另一个输入替换。"""
    payload = _result()
    if input_state == "absent":
        payload.pop("inputs")
    else:
        payload["inputs"] = None
    asyncio.run(_store(facts_database, result=payload))
    channel = AsyncMock(side_effect=AssertionError("historical preview must not connect to a channel"))
    monkeypatch.setattr(account_feishu, "query_account_assets", channel)

    async def preview():
        return await account_feishu._load_notification_preview_context(build_account(portfolio_id=None))

    context = asyncio.run(preview())
    result = asyncio.run(_collect(facts_database)).executions[_EXECUTION].record["result"]

    assert ("inputs" in result) is (input_state == "null")
    if input_state == "null":
        assert result["inputs"] is None
    assert business_evidence(context["targets"]["current"]) == business_evidence(
        _input()["curr_target"] if input_state == "absent" else {}
    )
    channel.assert_not_awaited()


@pytest.mark.parametrize("profile", [EvidenceProfile.SHAREABLE, EvidenceProfile.INTERNAL])
def test_archive_contains_canonical_facts_and_resolvable_sources(facts_database, profile):
    """审计下载可追溯至真实重建视图，且文件校验值覆盖实际内容。"""
    payload = _result()
    escaped = "rb/2610~native"
    payload["symbol_results"][escaped] = payload["symbol_results"].pop("rb2610")
    payload["extra"] = {"api_key": "synthetic-archive-secret", "plugin_diagnostic": "ordinary-native-diagnostic"}
    asyncio.run(_store(facts_database, result=payload))
    snapshot = asyncio.run(_collect(facts_database))
    bundle = build_archive(snapshot, profile, ExportBudget())
    try:
        with ZipFile(bundle.path) as archive:
            document = json.loads(archive.read(f"executions/{_EXECUTION}/record.json"))
            manifest = json.loads(archive.read("manifest.json"))
            assert "raw_input" not in document and "raw_result" not in document
            assert business_evidence(document["input"]["curr_target"]["zero-target"]) == business_evidence(0)
            assert document["result"]["account_assets"]["positions"][0]["market_value"] is None
            assert business_evidence(document["result"]["execution_time"]) == business_evidence(0.0)
            assert business_evidence(
                document["result"]["symbol_results"][escaped]["trades"][0]["extra"]["commission"]
            ) == business_evidence(0)
            assert manifest["schema_version"] >= 3
            all_text = b"\n".join(archive.read(name) for name in archive.namelist()).decode("utf-8-sig")
            assert "synthetic-archive-secret" not in all_text
            assert ("ordinary-native-diagnostic" in all_text) is (profile == EvidenceProfile.INTERNAL)
            for collection, native_id in (("orders", "order_id"), ("trades", "trade_id")):
                rows = _csv(archive, f"executions/{_EXECUTION}/{collection}.csv")
                assert business_evidence(len(rows)) == business_evidence(1)
                pointer = rows[0]["source_path"]
                filename, fragment = pointer.split("#", 1)
                assert business_evidence(filename) == business_evidence("record.json")
                assert fragment.startswith("/result/")
                assert "rb~12610~0native" in fragment
                source = _resolve_pointer(document, fragment)
                assert business_evidence(source[native_id]) == business_evidence(rows[0][native_id])
            entries = {entry["path"]: entry for entry in manifest["files"]}
            record_source = entries[f"executions/{_EXECUTION}/record.json"]["source"]
            assert "assembled" in record_source or "business_fact" in record_source
            for name, entry in entries.items():
                content = archive.read(name)
                assert business_evidence(entry["bytes"]) == business_evidence(len(content))
                assert business_evidence(entry["sha256"]) == business_evidence(hashlib.sha256(content).hexdigest())
    finally:
        bundle.close()


@pytest.mark.parametrize("collection", ["orders", "positions"])
def test_evidence_budget_counts_child_rows_before_materializing_full_result(facts_database, monkeypatch, collection):
    """一条结果包含大量订单或持仓时，不能只按父记录计行。"""
    payload = _result()
    if collection == "orders":
        order = payload["symbol_results"]["rb2610"]["orders"][0]
        payload["symbol_results"]["rb2610"]["orders"] = [
            {**order, "order_id": f"native-{index}"} for index in range(100)
        ]
    else:
        position = payload["account_assets"]["positions"][0]
        payload["account_assets"]["positions"] = [deepcopy(position) for _ in range(100)]
    asyncio.run(_store(facts_database, result=payload))
    detail_reader = AsyncMock(side_effect=AssertionError("budget preflight must precede cold detail assembly"))
    input_reader = AsyncMock(side_effect=AssertionError("budget preflight must precede cold input assembly"))
    monkeypatch.setattr(fact_repository, "read_execution_detail", detail_reader)
    monkeypatch.setattr(fact_repository, "read_execution_input", input_reader)
    budget = ExportBudget(limits=replace(EvidenceLimits(), rows=25))

    with pytest.raises(EvidenceError) as error:
        asyncio.run(_collect(facts_database, budget))

    assert business_evidence(error.value.status) == business_evidence(413)
    assert business_evidence(error.value.code) == business_evidence("EVIDENCE_TOO_LARGE")
    detail_reader.assert_not_awaited()
    input_reader.assert_not_awaited()


def test_evidence_budget_counts_scoped_extension_bytes(facts_database):
    """扩展虽是允许的 JSON，其读取体积仍属于证据预算。"""
    payload = _result()
    payload["extra"]["plugin_diagnostic"] = "synthetic native diagnostic " * 10000
    asyncio.run(_store(facts_database, result=payload))
    budget = ExportBudget(limits=replace(EvidenceLimits(), bytes=16000))

    with pytest.raises(EvidenceError) as error:
        asyncio.run(_collect(facts_database, budget))

    assert business_evidence(error.value.status) == business_evidence(413)
    assert business_evidence(error.value.code) == business_evidence("EVIDENCE_TOO_LARGE")


def test_oversized_malformed_child_json_is_rejected_before_decoding(facts_database):
    """损坏扩展超过预算时直接拒绝，不能先解码而触发 JSON 错误。"""
    record_id = asyncio.run(_store(facts_database))

    async def corrupt_and_collect():
        sql(
            facts_database,
            "UPDATE execution_extension SET extra=? WHERE record_id=?",
            ("malformed-extension" * 10000, record_id),
        )
        return await _collect(facts_database, ExportBudget(limits=replace(EvidenceLimits(), bytes=16000)))

    with pytest.raises(EvidenceError) as error:
        asyncio.run(corrupt_and_collect())

    assert business_evidence(error.value.status) == business_evidence(413)
    assert business_evidence(error.value.code) == business_evidence("EVIDENCE_TOO_LARGE")


async def _store_preterminal_observations(path):
    before = deepcopy(_result()["account_assets"])
    before.update(total_asset=100001.0, positions=[], update_time="2026-10-20T09:00:00+08:00")
    after = {
        "available_cash": 0,
        "total_asset": 0,
        "market_value": None,
        "positions": None,
        "source": "unavailable",
        "update_time": "2026-10-20T09:00:09+08:00",
    }
    input_id = freeze_input(
        path,
        _preterminal_input(),
        execution_id=_EXECUTION,
        created_at="2026-10-20T09:00:00+08:00",
        before_assets=before,
    )["input_id"]
    native_store_call(
        path,
        "freeze_execution_after_assets",
        {
            "account_id": 1,
            "execution_id": _EXECUTION,
            "payload": {"account_assets": after},
        },
    )
    _seed(path, _running_intent())
    for view, kind, asset_id in (
        ("input", ExecutionArtifactType.STANDARD_INPUT, None),
        ("targets", ExecutionArtifactType.TARGET_SNAPSHOT, None),
        ("rules", ExecutionArtifactType.TRADE_RULES_SNAPSHOT, None),
        ("assets_before", ExecutionArtifactType.ACCOUNT_SNAPSHOT_BEFORE, input_asset_fact_id(input_id)),
        ("assets_after", ExecutionArtifactType.ACCOUNT_SNAPSHOT, input_after_asset_fact_id(input_id)),
    ):
        _seed(
            path,
            ExecutionArtifact(
                execution_id=_EXECUTION,
                artifact_type=kind,
                schema_version=3,
                content=make_artifact_reference(view=view, input_id=input_id, asset_id=asset_id),
            ),
        )
    return input_id, before, after


def _running_intent():
    return (
        "execution_intent",
        dict(
            account_id=1,
            execution_id=_EXECUTION,
            kind=ExecutionKind.REBALANCE,
            trigger_source="manual",
            status=ExecutionTaskStatus.RUNNING,
            channel="ctp",
            algorithm="SINGLE-MAKER",
            created_at="2026-10-20T09:00:00+08:00",
            started_at="2026-10-20T09:00:01+08:00",
        ),
    )


def _preterminal_input() -> dict:
    payload = _input()
    payload.update(
        account_target={"rb2610": 0.125},
        strategy_target={"rb2610": 0.5},
        trade_rules={"rb2610": {"min_order_volume": 1, "volume_multiple": 10}},
        futures_close_policy={"mode": "rules", "fallback": "today_first", "overrides": {"RB": "yesterday_first"}},
    )
    return payload


def test_preterminal_input_and_both_asset_roles_are_exportable_without_result(facts_database):
    """交易前已提交的输入与两次资产观测不依赖终态结果记录。"""
    input_id, before, after = asyncio.run(_store_preterminal_observations(facts_database))
    snapshot = asyncio.run(_collect(facts_database))
    item = snapshot.executions[_EXECUTION]
    assert item.record is None
    artifacts = {row["artifact_type"]: row["content"] for row in item.artifacts}
    assert business_evidence(artifacts["standard_input"]["input"]) == business_evidence(_preterminal_input())
    assert business_evidence(artifacts["target_snapshot"]["curr_target"]["zero-target"]) == business_evidence(0)
    assert business_evidence(
        artifacts["target_snapshot"]["audit_evidence"]["unrecognized_fields"]["target_conflicts"]["account_target"]
    ) == business_evidence({"rb2610": 0.125})
    assert business_evidence(artifacts["target_snapshot"]["strategy_target"]) == business_evidence({"rb2610": 0.5})
    assert business_evidence(artifacts["trade_rules_snapshot"]["trade_rules"]) == business_evidence(
        _preterminal_input()["trade_rules"]
    )
    assert business_evidence(artifacts["trade_rules_snapshot"]["futures_close_policy"]) == business_evidence(
        _preterminal_input()["futures_close_policy"]
    )
    assert business_evidence(artifacts["account_snapshot_before"]["account_assets"]) == business_evidence(before)
    assert business_evidence(artifacts["account_snapshot"]["account_assets"]) == business_evidence(after)
    assert business_evidence(artifacts["account_snapshot"]["business_facts_ref"]["input_id"]) == business_evidence(
        input_id
    )
    assert business_evidence(snapshot.source_counts["execution_input"]) == business_evidence(1)
    assert business_evidence(snapshot.source_counts["business_asset_snapshot"]) == business_evidence(2)
    bundle = build_archive(snapshot, EvidenceProfile.INTERNAL, ExportBudget())
    try:
        with ZipFile(bundle.path) as archive:
            assert json.loads(archive.read(f"executions/{_EXECUTION}/record.json")) is None
            exported = json.loads(archive.read(f"executions/{_EXECUTION}/artifacts.json"))
            contents = {row["artifact_type"]: row["content"] for row in exported}
            assert business_evidence(
                contents["account_snapshot_before"]["account_assets"]["positions"]
            ) == business_evidence([])
            assert contents["account_snapshot"]["account_assets"]["positions"] is None
            assert business_evidence(_csv(archive, f"executions/{_EXECUTION}/trades.csv")) == business_evidence([])
    finally:
        bundle.close()


@pytest.mark.parametrize("with_target_event", [False, True])
def test_pending_frozen_input_is_exported_even_without_artifact(facts_database, with_target_event):
    """输入真源独立存在；缺失的审计附件仍应如实报告。"""
    before = deepcopy(_result()["account_assets"])
    before.update(total_asset=100001.0, positions=[], update_time="2026-10-20T09:00:00+08:00")
    after = deepcopy(_result()["account_assets"])
    after.update(total_asset=90000.0, update_time="2026-10-20T09:00:09+08:00")

    async def seed():
        input_id = freeze_input(
            facts_database,
            _preterminal_input(),
            execution_id=_EXECUTION,
            created_at="2026-10-20T09:00:00+08:00",
            before_assets=before,
        )["input_id"]
        native_store_call(
            facts_database,
            "freeze_execution_after_assets",
            {
                "account_id": 1,
                "execution_id": _EXECUTION,
                "payload": {"account_assets": after},
            },
        )
        _seed(facts_database, _running_intent())
        if with_target_event:
            _seed(
                facts_database,
                ExecutionEvent(
                    execution_id=_EXECUTION,
                    account_id=1,
                    channel="ctp",
                    algorithm="SINGLE-MAKER",
                    event_uid="facts-target-event",
                    event_type=ExecutionEventType.TARGET_COMPUTED,
                    status=ExecutionEventStatus.INFO,
                    reason_family=ExecutionReasonFamily.INPUT,
                    reason_code="COMMON.TARGET_COMPUTED",
                    details={
                        "decision": make_artifact_reference(
                            view="targets", input_id=input_id, select_fields=["curr_target"]
                        )
                    },
                ),
            )
        return input_id

    input_id = asyncio.run(seed())
    snapshot = asyncio.run(_collect(facts_database))
    item = snapshot.executions[_EXECUTION]
    assert business_evidence(item.input_id) == business_evidence(input_id)
    assert business_evidence(item.input) == business_evidence(_preterminal_input())
    assert item.record is None and item.artifacts == []
    observations = {row["asset_id"]: row for row in item.asset_observations}
    assert business_evidence(observations[input_asset_fact_id(input_id)]["account_assets"]) == business_evidence(before)
    assert business_evidence(observations[input_after_asset_fact_id(input_id)]["account_assets"]) == business_evidence(
        after
    )
    assert all(row["input_id"] == input_id for row in observations.values())
    if with_target_event:
        decision = item.events[0]["details"]["decision"]
        assert business_evidence(
            {key: value for key, value in decision.items() if key != "business_facts_ref"}
        ) == business_evidence({"curr_target": _preterminal_input()["curr_target"]})
        assert business_evidence(decision["business_facts_ref"]["input_id"]) == business_evidence(input_id)
    bundle = build_archive(snapshot, EvidenceProfile.INTERNAL, ExportBudget())
    try:
        with ZipFile(bundle.path) as archive:
            inputs = json.loads(archive.read(f"executions/{_EXECUTION}/input.json"))
            assert business_evidence(inputs) == business_evidence({"input_id": input_id, "input": _preterminal_input()})
            assert json.loads(archive.read(f"executions/{_EXECUTION}/record.json")) is None
            assets = json.loads(archive.read(f"executions/{_EXECUTION}/asset-observations.json"))
            assets = {row["asset_id"]: row for row in assets}
            assert business_evidence(assets[input_asset_fact_id(input_id)]["account_assets"]) == business_evidence(
                before
            )
            assert business_evidence(
                assets[input_after_asset_fact_id(input_id)]["account_assets"]
            ) == business_evidence(after)
            manifest = json.loads(archive.read("manifest.json"))
            coverage = manifest["executions"][0]["coverage"]
            assert business_evidence(coverage["coverage"]) == business_evidence("provisional")
            assert "standard_input" in coverage["missing_artifacts"]
            if with_target_event:
                events = [
                    json.loads(line) for line in archive.read(f"executions/{_EXECUTION}/events.jsonl").splitlines()
                ]
                decision = events[0]["details"]["decision"]
                assert business_evidence(
                    {key: value for key, value in decision.items() if key != "business_facts_ref"}
                ) == business_evidence({"curr_target": _preterminal_input()["curr_target"]})
    finally:
        bundle.close()


@pytest.mark.parametrize("fault", ["account", "execution", "observation_role"])
def test_asset_reference_rejects_wrong_owner_or_observation_role(facts_database, fault):
    """事实身份相同也不能越过账户、执行和前后观测时点边界。"""
    input_id, _, _ = asyncio.run(_store_preterminal_observations(facts_database))
    content = make_artifact_reference(
        view="assets_after", input_id=input_id, asset_id=input_after_asset_fact_id(input_id)
    )
    if fault == "observation_role":
        content["business_facts_ref"]["view"] = "assets_before"
    artifact = {
        "execution_id": "different-execution" if fault == "execution" else _EXECUTION,
        "artifact_type": "account_snapshot_before" if fault == "observation_role" else "account_snapshot",
        "content": content,
    }

    async def resolve():
        return await resolve_artifact_content(artifact, account_id=2 if fault == "account" else 1)

    with pytest.raises(BusinessFactReferenceError, match="时点" if fault == "observation_role" else "执行账户"):
        asyncio.run(resolve())


def test_business_storage_has_no_complete_raw_document_columns(facts_database):
    """持久化结果经新事实表可读取，数据库不再保存完整旧文档。"""
    asyncio.run(_store(facts_database))

    async def inspect_storage():
        engine = sa.create_engine(f"sqlite:///{facts_database}")
        try:
            columns = sa.inspect(engine).get_columns("executerecord")
        finally:
            engine.dispose()
        record = _get(facts_database, "executerecord")
        detail = await read_execution_detail(record)
        return columns, detail

    columns, detail = asyncio.run(inspect_storage())

    assert {column["name"] for column in columns}.isdisjoint({"raw_input", "raw_result"})
    assert business_evidence(detail["result"]["error"]) == business_evidence(_TECHNICAL_ERROR)
    assert business_evidence(
        detail["result"]["symbol_results"]["rb2610"]["trades"][0]["trade_value"]
    ) == business_evidence(35000.125)


def _summary_result() -> dict:
    payload = _result()
    payload["symbol_results"]["rb2610"]["orders"][0]["extra"]["native_observation"] = "observed callback frame\n" * 128
    return payload


def _pending_summary(payload: dict) -> dict:
    """worker 在终态记录之前保存的完整 journal，含独立对账证据。"""
    return {
        "status": payload["status"],
        "error": payload["error"],
        "outcome": payload["outcome"],
        "outcome_reason": payload["outcome_reason"],
        "symbol_results": deepcopy(payload["symbol_results"]),
        "summary": {"symbols_total": 1, "symbols_failed": 1},
        "success": False,
        "execution_time": 0.0,
        "reconciliation": {
            "account": {"source_before": "real", "source_after": "real"},
            "symbols": [{"symbol": "rb2610", "before": 0, "target": 2, "after": 1, "reached": False}],
        },
        "diagnostic": {"marker": "independent-worker-observation", "attempt": 0},
    }


async def _seed_pending_summary(path, summary):
    freeze_input(path, _input(), execution_id=_EXECUTION, created_at="2026-10-20T09:00:00+08:00")
    _seed(
        path,
        _running_intent(),
        ExecutionArtifact(
            execution_id=_EXECUTION,
            artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
            schema_version=2,
            content=deepcopy(summary),
        ),
    )


async def _write_real_terminal(path, payload):
    return await records._persist_execute_record(
        account_id=1, execution_id=_EXECUTION, raw_input=_input(), raw_result=deepcopy(payload), is_success=0
    )


def test_terminal_commit_freezes_full_summary_and_preserves_evidence(facts_database):
    """终态及完整摘要同事务提交，冷读和下载仍保留原始 journal 内容。"""
    payload = _summary_result()
    journal = _pending_summary(payload)
    asyncio.run(_seed_pending_summary(facts_database, journal))
    asyncio.run(_write_real_terminal(facts_database, payload))

    async def read_summary():
        artifact = _get(facts_database, "execution_artifact")
        stored = deepcopy(artifact.content)
        hydrated = await resolve_artifact_content(artifact, account_id=1)
        return stored, hydrated

    stored, hydrated = asyncio.run(read_summary())
    assert "business_facts_ref" not in stored
    assert stored == hydrated
    assert business_evidence(hydrated) == business_evidence(journal)
    snapshot = asyncio.run(_collect(facts_database))
    bundle = build_archive(snapshot, EvidenceProfile.INTERNAL, ExportBudget())
    try:
        with ZipFile(bundle.path) as archive:
            artifacts = json.loads(archive.read(f"executions/{_EXECUTION}/artifacts.json"))
            summary = next(row["content"] for row in artifacts if row["artifact_type"] == "execution_summary")
            summary.pop("business_facts_ref", None)
            assert business_evidence(summary) == business_evidence(journal)
            assert business_evidence(
                _csv(archive, f"executions/{_EXECUTION}/trades.csv")[0]["trade_id"]
            ) == business_evidence("native-fill-01")
    finally:
        bundle.close()


def test_failed_terminal_transaction_keeps_original_worker_journal(facts_database):
    """终态提交失败时，已保存的 worker 摘要保持原文。"""
    payload = _summary_result()
    journal = _pending_summary(payload)
    asyncio.run(_seed_pending_summary(facts_database, journal))

    sql(
        facts_database,
        """CREATE TRIGGER reject_terminal AFTER UPDATE OF facts_complete ON executerecord
        WHEN NEW.facts_complete=1
        BEGIN SELECT RAISE(ABORT,'synthetic terminal persistence failure'); END""",
    )
    with pytest.raises(_core.NativeError, match="terminal persistence failure"):
        asyncio.run(_write_real_terminal(facts_database, payload))

    async def read_after_rollback():
        count = len(rows(facts_database, "executerecord"))
        artifact = _get(facts_database, "execution_artifact")
        return count, artifact.schema_version, artifact.content

    count, version, retained = asyncio.run(read_after_rollback())
    assert business_evidence(count) == business_evidence(0)
    assert business_evidence(version) == business_evidence(2)
    assert business_evidence(retained) == business_evidence(journal)
    snapshot = asyncio.run(_collect(facts_database))
    assert snapshot.executions[_EXECUTION].record is None
    assert business_evidence(snapshot.executions[_EXECUTION].artifacts[0]["content"]) == business_evidence(journal)


def test_terminal_compaction_keeps_distinct_journal_observation(facts_database):
    """不同的历史观测不能被最终结果引用覆盖。"""
    payload = _summary_result()
    journal = _pending_summary(payload)
    journal["symbol_results"]["rb2610"]["orders"][0]["filled_volume"] = 0
    asyncio.run(_seed_pending_summary(facts_database, journal))
    asyncio.run(_write_real_terminal(facts_database, payload))

    async def read_summary():
        artifact = _get(facts_database, "execution_artifact")
        return artifact.content, await resolve_artifact_content(artifact, account_id=1)

    stored, hydrated = asyncio.run(read_summary())
    assert business_evidence(stored["symbol_results"]["rb2610"]["orders"]) == business_evidence(
        journal["symbol_results"]["rb2610"]["orders"]
    )
    hydrated.pop("business_facts_ref", None)
    assert business_evidence(hydrated) == business_evidence(journal)


def _large_captured_notification() -> dict:
    """捕获时的业务快照故意与当前账户名称不同，防止冷读重新推算。"""
    payload = _result()
    return {
        "is_test": False,
        "account": {"id": 1, "name": "frozen-at-execution"},
        "execution": {"status": "PARTIAL", "success": False, "error": _TECHNICAL_ERROR, "execution_time": 0.0},
        "assets": {"available_cash": 0.0, "total_assets": None, "market_value": None},
        "targets": {"current": {"rb2610": 0.25}, "previous": {}},
        "positions": [deepcopy(payload["account_assets"]["positions"][0]) for _ in range(32)],
        "orders": [deepcopy(payload["symbol_results"]["rb2610"]["orders"][0]) for _ in range(32)],
        "trades": [deepcopy(payload["symbol_results"]["rb2610"]["trades"][0]) for _ in range(32)],
    }


async def _seed_supplement_step(path):
    _seed(
        path,
        SupplementGroup(
            id="facts-notification-group",
            account_id=1,
            fingerprint="facts-notification-fingerprint",
            base_scheduled_at="2026-10-20T09:00:00+08:00",
            expires_at="2026-10-20T23:00:00+08:00",
            configured_count=1,
            effective_count=1,
        ),
        SupplementStep(
            id="facts-notification-step",
            group_id="facts-notification-group",
            index=1,
            scheduled_at="2026-10-20T09:00:00+08:00",
            status="running",
            execution_id=_EXECUTION,
        ),
    )


async def _stored_captured_notification(path):
    return rows(path, "execution_extension")[0]["extra"]["server_notification_context"]


@pytest.mark.parametrize("existing_event", [False, True])
def test_finished_outbox_owns_large_context_and_preview_keeps_frozen_snapshot(
    facts_database, monkeypatch, existing_event
):
    """outbox 冻结成功后保留独立捕获快照；已有通知正文与预览优先级保持不变。"""
    captured = _large_captured_notification()
    payload = _result()
    payload["extra"]["server_notification_context"] = deepcopy(captured)
    original_event = deepcopy(captured)
    original_event.update(event_id=f"execution:{_EXECUTION}", event_type="execution.finished", is_test=False)
    original_event["execution"].update(id=_EXECUTION, notified_at=_CREATED, is_test=False)
    asyncio.run(_store(facts_database, result=payload, event_context=original_event if existing_event else None))
    asyncio.run(_seed_supplement_step(facts_database))

    async def finish():
        await _finish(facts_database)
        stored = await _stored_captured_notification(facts_database)
        event = _get(facts_database, "notification_event")
        record = _get(facts_database, "executerecord")
        detail = await read_execution_detail(record)
        return stored, event.context, detail

    stored, event_context, detail = asyncio.run(finish())
    assert stored == captured
    assert business_evidence(detail["result"]["extra"]["server_notification_context"]) == business_evidence(captured)
    if existing_event:
        assert business_evidence(event_context) == business_evidence(original_event)
    for key in ("account", "assets", "targets", "positions", "orders", "trades"):
        assert business_evidence(event_context[key]) == business_evidence(captured[key])

    forbidden_detail = AsyncMock(side_effect=AssertionError("frozen outbox must be selected before canonical detail"))
    monkeypatch.setattr(fact_repository, "read_execution_detail", forbidden_detail)

    async def preview():
        context = await account_feishu._load_notification_preview_context(build_account(portfolio_id=None))
        assert business_evidence(context["account"]["name"]) == business_evidence("frozen-at-execution")
        assert business_evidence(context["orders"]) == business_evidence(captured["orders"])
        context["orders"][0]["order_id"] = "preview-only-edit"
        event = _get(facts_database, "notification_event")
        return event.context

    assert business_evidence(asyncio.run(preview())) == business_evidence(event_context)
    forbidden_detail.assert_not_awaited()


def test_context_compaction_rollback_retains_only_copy_when_outbox_is_not_committed(facts_database):
    """通知提交失败时，捕获正文、步骤状态和 outbox 一起回滚。"""
    captured = _large_captured_notification()
    payload = _result()
    payload["extra"]["server_notification_context"] = deepcopy(captured)
    asyncio.run(_store(facts_database, result=payload))
    asyncio.run(_seed_supplement_step(facts_database))

    sql(
        facts_database,
        """CREATE TRIGGER reject_notification AFTER INSERT ON notification_event
        BEGIN SELECT RAISE(ABORT,'synthetic notification transaction failure'); END""",
    )
    with pytest.raises(_core.NativeError, match="notification transaction failure"):
        asyncio.run(_finish(facts_database))

    async def inspect_after_rollback():
        event = _get(facts_database, "notification_event")
        step = _get(facts_database, "supplement_step")
        return event, step.status, await _stored_captured_notification(facts_database)

    event, step_status, retained = asyncio.run(inspect_after_rollback())
    assert event is None
    assert business_evidence(step_status) == business_evidence("running")
    assert business_evidence(retained) == business_evidence(captured)


def test_distinct_frozen_outbox_wins_preview_without_erasing_captured_evidence(facts_database, monkeypatch):
    """已发送快照与捕获快照不同，分别保留，预览继续取原 outbox。"""
    captured = _large_captured_notification()
    outbox = deepcopy(captured)
    outbox["orders"][0]["order_id"] = "independent-frozen-outbox-order"
    outbox["account"]["name"] = "frozen-outbox-account"
    payload = _result()
    payload["extra"]["server_notification_context"] = deepcopy(captured)
    asyncio.run(_store(facts_database, result=payload, event_context=outbox))
    asyncio.run(_seed_supplement_step(facts_database))

    async def finish():
        await _finish(facts_database)
        event = _get(facts_database, "notification_event")
        return await _stored_captured_notification(facts_database), event.context

    stored, retained_outbox = asyncio.run(finish())
    assert business_evidence(stored) == business_evidence(captured)
    assert business_evidence(retained_outbox) == business_evidence(outbox)
    forbidden_detail = AsyncMock(side_effect=AssertionError("existing outbox must win preview"))
    monkeypatch.setattr(fact_repository, "read_execution_detail", forbidden_detail)

    async def preview():
        return await account_feishu._load_notification_preview_context(build_account(portfolio_id=None))

    context = asyncio.run(preview())
    assert business_evidence(context["account"]["name"]) == business_evidence("frozen-outbox-account")
    assert business_evidence(context["orders"][0]["order_id"]) == business_evidence("independent-frozen-outbox-order")
    forbidden_detail.assert_not_awaited()


def test_no_outbox_keeps_captured_notification_as_unique_historical_evidence(facts_database, monkeypatch):
    """无补发步骤且未形成 outbox，不能删除唯一冻结通知来换取去重。"""
    captured = _large_captured_notification()
    payload = _result()
    payload["extra"]["server_notification_context"] = deepcopy(captured)
    asyncio.run(_store(facts_database, result=payload))
    channel = AsyncMock(side_effect=AssertionError("historical preview must not connect to a channel"))
    monkeypatch.setattr(account_feishu, "query_account_assets", channel)

    async def finish_without_step_and_preview():
        await _finish(facts_database)
        event = _get(facts_database, "notification_event")
        stored = await _stored_captured_notification(facts_database)
        preview = await account_feishu._load_notification_preview_context(build_account(portfolio_id=None))
        return event, stored, preview

    event, stored, preview = asyncio.run(finish_without_step_and_preview())
    assert event is None
    assert business_evidence(stored) == business_evidence(captured)
    assert business_evidence(preview["account"]["name"]) == business_evidence("frozen-at-execution")
    assert business_evidence(preview["positions"]) == business_evidence(captured["positions"])
    assert business_evidence(preview["orders"]) == business_evidence(captured["orders"])
    channel.assert_not_awaited()


@pytest.mark.parametrize("malformed", [False, True])
def test_referenced_outbox_bytes_are_budgeted_before_canonical_hydration(facts_database, monkeypatch, malformed):
    """捕获正文与 outbox 的大列仍需在解码和冷重建前计入预算。"""
    captured = _large_captured_notification()
    for key in ("positions", "orders", "trades"):
        captured[key] *= 8
    payload = _result()
    payload["extra"]["server_notification_context"] = captured
    asyncio.run(_store(facts_database, result=payload))
    asyncio.run(_seed_supplement_step(facts_database))

    async def finish_and_optionally_corrupt():
        await _finish(facts_database)
        stored = await _stored_captured_notification(facts_database)
        assert stored == captured
        with closing(oracle_connect(facts_database)) as connection:
            raw_size = connection.execute(
                "SELECT length(CAST(context AS BLOB)) FROM notification_event WHERE id=?", (f"execution:{_EXECUTION}",)
            ).fetchone()[0]
        assert raw_size > 64000
        if malformed:
            with closing(oracle_connect(facts_database)) as connection, connection:
                connection.execute(
                    "UPDATE notification_event SET context=? WHERE id=?",
                    ("malformed-notification-context" * 10000, f"execution:{_EXECUTION}"),
                )

    asyncio.run(finish_and_optionally_corrupt())
    cold_reader = AsyncMock(side_effect=AssertionError("outbox budget preflight must precede canonical hydration"))
    monkeypatch.setattr(fact_repository, "read_execution_detail", cold_reader)
    budget = ExportBudget(limits=replace(EvidenceLimits(), bytes=64000))

    with pytest.raises(EvidenceError) as error:
        asyncio.run(_collect(facts_database, budget))

    assert business_evidence(error.value.code) == business_evidence("EVIDENCE_TOO_LARGE")
    assert business_evidence(error.value.status) == business_evidence(413)
    cold_reader.assert_not_awaited()
