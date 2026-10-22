"""真实事实表到通知及证据归档的独立离线回归。"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

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
from axile.server.api.routes import account_feishu
from axile.server.business_facts import repository as fact_repository
from axile.server.business_facts.artifacts import (
    BusinessFactReferenceError,
    make_artifact_reference,
    resolve_artifact_content,
)
from axile.server.business_facts.assets import (
    encode_input_after_assets,
    input_after_asset_fact_id,
    input_asset_fact_id,
)
from axile.server.business_facts.repository import (
    persist_execution_input,
    read_execution_detail,
    store_execution_facts,
)
from axile.server.core.db import register_sqlite_concurrency_pragmas
from axile.server.db.models import ExecuteRecord
from axile.server.db.models.execution import ExecutionArtifact, ExecutionEvent
from axile.server.db.models.execution_intent import ExecutionIntent
from axile.server.db.models.supplement import NotificationEvent, SupplementGroup, SupplementStep
from axile.server.evidence.archive import build_archive
from axile.server.evidence.collect import collect_evidence
from axile.server.evidence.models import EvidenceError, EvidenceLimits, EvidenceProfile, ExportBudget
from axile.server.execution import records, supplement_notifications
from scripts.business_facts_contract import business_evidence
from tests.unit.server._execution_test_support import build_account

_EXECUTION = "facts-notification-evidence"
_CREATED = "2026-10-20T09:00:10+08:00"
_TECHNICAL_ERROR = "RuntimeError: native callback failed\n" + "  at channel.confirm(order_id='native-01')\n" * 128


@pytest.fixture
def facts_database(tmp_path, monkeypatch):
    """独立 SQLite；测试只调用本地事实读取与归档。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'facts-evidence.db'}", poolclass=NullPool)
    register_sqlite_concurrency_pragmas(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def initialize():
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with factory() as session:
            session.add(build_account(portfolio_id=None))
            await session.commit()

    asyncio.run(initialize())
    store = account_feishu._core.Store(str(tmp_path / "facts-evidence.db"))
    monkeypatch.setattr(account_feishu, "analysis_store", lambda: store)
    yield factory
    asyncio.run(engine.dispose())


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


async def _store(factory, *, result=None, event_context=None):
    payload = _result() if result is None else result
    async with factory() as session:
        record = ExecuteRecord(account_id=1, execution_id=_EXECUTION, created_at=_CREATED, is_success=0)
        session.add(record)
        await session.flush()
        await store_execution_facts(session, record, raw_input=_input(), raw_result=deepcopy(payload))
        session.add(
            ExecutionIntent(
                account_id=1,
                execution_id=_EXECUTION,
                kind=ExecutionKind.REBALANCE,
                trigger_source="manual",
                status=ExecutionTaskStatus.PARTIAL,
                channel="ctp",
                algorithm="SINGLE-MAKER",
                created_at="2026-10-20T09:00:00+08:00",
                started_at="2026-10-20T09:00:01+08:00",
                finished_at=_CREATED,
            )
        )
        if event_context is not None:
            session.add(
                NotificationEvent(
                    id=f"execution:{_EXECUTION}",
                    account_id=1,
                    event_type="execution.finished",
                    context=deepcopy(event_context),
                )
            )
        await session.commit()
        return record.id


async def _collect(factory, budget=None):
    async with factory() as session:
        return await collect_evidence(session, budget or ExportBudget(), execution_id=_EXECUTION)


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
    previous = feishu_notifications.build_execution_notification_context(
        account_feishu._TestNotificationSource(account.name), UnifiedStandardOutput.model_validate(old_payload)
    )
    asyncio.run(_store(facts_database, result=payload))
    channel = AsyncMock(side_effect=AssertionError("historical preview must not connect to a channel"))
    monkeypatch.setattr(account_feishu, "query_account_assets", channel)

    async def preview():
        async with facts_database() as session:
            return await account_feishu._load_notification_preview_context(session, account)

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
        async with facts_database() as session:
            context = await account_feishu._load_notification_preview_context(session, build_account(portfolio_id=None))
            context["orders"][0]["extra"]["marker"] = "changed"
            context["execution"]["supplements"][0]["is_last"] = False
            await session.flush()
            if context_source == "outbox":
                event = await session.scalar(
                    select(NotificationEvent).where(NotificationEvent.id == f"execution:{_EXECUTION}")
                )
                return event.context
            record = await session.scalar(select(ExecuteRecord).where(ExecuteRecord.execution_id == _EXECUTION))
            detail = await read_execution_detail(session, record)
            return detail["result"]["extra"]["server_notification_context"]

    assert business_evidence(asyncio.run(inspect_preview())) == business_evidence(saved)


def test_supplement_notification_keeps_full_error_after_facts_roundtrip(facts_database):
    """终态补发仍使用完整错误字符串并保留资产缺失语义。"""
    asyncio.run(_store(facts_database))

    async def context():
        async with facts_database() as session:
            row = await session.scalar(select(ExecutionIntent).where(ExecutionIntent.execution_id == _EXECUTION))
            return await supplement_notifications._execution_context(session, row, build_account(portfolio_id=None))

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
        async with facts_database() as session:
            return await account_feishu._load_notification_preview_context(session, build_account(portfolio_id=None))

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
        async with facts_database() as session:
            return await account_feishu._load_notification_preview_context(session, build_account(portfolio_id=None))

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
        async with facts_database() as session:
            await session.execute(
                text("UPDATE execution_extension SET extra=:invalid WHERE record_id=:record_id"),
                {"invalid": "malformed-extension" * 10000, "record_id": record_id},
            )
            await session.commit()
        return await _collect(facts_database, ExportBudget(limits=replace(EvidenceLimits(), bytes=16000)))

    with pytest.raises(EvidenceError) as error:
        asyncio.run(corrupt_and_collect())

    assert business_evidence(error.value.status) == business_evidence(413)
    assert business_evidence(error.value.code) == business_evidence("EVIDENCE_TOO_LARGE")


async def _store_preterminal_observations(factory):
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
    async with factory() as session:
        input_id = await persist_execution_input(
            session,
            account_id=1,
            execution_id=_EXECUTION,
            payload=_preterminal_input(),
            created_at="2026-10-20T09:00:00+08:00",
            before_assets=before,
        )
        for table, values in encode_input_after_assets(input_id, {"account_assets": after}).items():
            if values:
                await session.execute(table.insert(), values)
        session.add(
            ExecutionIntent(
                account_id=1,
                execution_id=_EXECUTION,
                kind=ExecutionKind.REBALANCE,
                trigger_source="manual",
                status=ExecutionTaskStatus.RUNNING,
                channel="ctp",
                algorithm="SINGLE-MAKER",
                created_at="2026-10-20T09:00:00+08:00",
                started_at="2026-10-20T09:00:01+08:00",
            )
        )
        for view, kind, asset_id in (
            ("input", ExecutionArtifactType.STANDARD_INPUT, None),
            ("targets", ExecutionArtifactType.TARGET_SNAPSHOT, None),
            ("rules", ExecutionArtifactType.TRADE_RULES_SNAPSHOT, None),
            ("assets_before", ExecutionArtifactType.ACCOUNT_SNAPSHOT_BEFORE, input_asset_fact_id(input_id)),
            ("assets_after", ExecutionArtifactType.ACCOUNT_SNAPSHOT, input_after_asset_fact_id(input_id)),
        ):
            session.add(
                ExecutionArtifact(
                    execution_id=_EXECUTION,
                    artifact_type=kind,
                    schema_version=3,
                    content=make_artifact_reference(view=view, input_id=input_id, asset_id=asset_id),
                )
            )
        await session.commit()
    return input_id, before, after


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
        async with facts_database() as session:
            input_id = await persist_execution_input(
                session,
                account_id=1,
                execution_id=_EXECUTION,
                payload=_preterminal_input(),
                created_at="2026-10-20T09:00:00+08:00",
                before_assets=before,
            )
            for table, values in encode_input_after_assets(input_id, {"account_assets": after}).items():
                if values:
                    await session.execute(table.insert(), values)
            session.add(
                ExecutionIntent(
                    account_id=1,
                    execution_id=_EXECUTION,
                    kind=ExecutionKind.REBALANCE,
                    trigger_source="manual",
                    status=ExecutionTaskStatus.RUNNING,
                    channel="ctp",
                    algorithm="SINGLE-MAKER",
                    created_at="2026-10-20T09:00:00+08:00",
                    started_at="2026-10-20T09:00:01+08:00",
                )
            )
            if with_target_event:
                session.add(
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
                    )
                )
            await session.commit()
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
        async with facts_database() as session:
            return await resolve_artifact_content(session, artifact, account_id=2 if fault == "account" else 1)

    with pytest.raises(BusinessFactReferenceError, match="时点" if fault == "observation_role" else "执行账户"):
        asyncio.run(resolve())


def test_business_storage_has_no_complete_raw_document_columns(facts_database):
    """持久化结果经新事实表可读取，数据库不再保存完整旧文档。"""
    record_id = asyncio.run(_store(facts_database))

    async def inspect_storage():
        async with facts_database() as session:
            connection = await session.connection()
            columns = await connection.run_sync(lambda conn: inspect(conn).get_columns("executerecord"))
            record = await session.get(ExecuteRecord, record_id)
            detail = await read_execution_detail(session, record)
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


async def _seed_pending_summary(factory, summary):
    async with factory() as session:
        await persist_execution_input(
            session, account_id=1, execution_id=_EXECUTION, payload=_input(), created_at="2026-10-20T09:00:00+08:00"
        )
        session.add(
            ExecutionIntent(
                account_id=1,
                execution_id=_EXECUTION,
                kind=ExecutionKind.REBALANCE,
                trigger_source="manual",
                status=ExecutionTaskStatus.RUNNING,
                channel="ctp",
                algorithm="SINGLE-MAKER",
                created_at="2026-10-20T09:00:00+08:00",
                started_at="2026-10-20T09:00:01+08:00",
            )
        )
        session.add(
            ExecutionArtifact(
                execution_id=_EXECUTION,
                artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
                schema_version=2,
                content=deepcopy(summary),
            )
        )
        await session.commit()


async def _write_real_terminal(factory, payload):
    return await records._persist_execute_record(
        account_id=1,
        execution_id=_EXECUTION,
        raw_input=_input(),
        raw_result=deepcopy(payload),
        is_success=0,
        session_factory=factory,
    )


def _isolate_analysis_projection(monkeypatch):
    """事务仍使用真实 SQLite；本组只隔离与审计去重无关的分析投影。"""
    monkeypatch.setattr(records, "write_observation_fact", AsyncMock())
    monkeypatch.setattr(records, "write_source_facts", AsyncMock())


def test_terminal_commit_compacts_pending_summary_and_hydrates_identical_evidence(facts_database, monkeypatch):
    """终态及摘要引用同事务提交，冷读和下载仍保留原始 journal 内容。"""
    _isolate_analysis_projection(monkeypatch)
    payload = _summary_result()
    journal = _pending_summary(payload)
    asyncio.run(_seed_pending_summary(facts_database, journal))
    record = asyncio.run(_write_real_terminal(facts_database, payload))

    async def read_summary():
        async with facts_database() as session:
            artifact = await session.scalar(
                select(ExecutionArtifact).where(
                    ExecutionArtifact.execution_id == _EXECUTION,
                    ExecutionArtifact.artifact_type == ExecutionArtifactType.EXECUTION_SUMMARY,
                )
            )
            stored = deepcopy(artifact.content)
            hydrated = await resolve_artifact_content(session, artifact, account_id=1)
            return stored, hydrated

    stored, hydrated = asyncio.run(read_summary())
    assert business_evidence(stored["business_facts_ref"]["view"]) == business_evidence("result")
    assert business_evidence(stored["business_facts_ref"]["record_id"]) == business_evidence(record.id)
    stored_symbol = stored.get("symbol_results", {}).get("rb2610", {})
    assert "orders" not in stored_symbol and "trades" not in stored_symbol
    assert len(json.dumps(stored)) < len(json.dumps(journal))
    hydrated.pop("business_facts_ref")
    assert business_evidence(hydrated) == business_evidence(journal)
    snapshot = asyncio.run(_collect(facts_database))
    bundle = build_archive(snapshot, EvidenceProfile.INTERNAL, ExportBudget())
    try:
        with ZipFile(bundle.path) as archive:
            artifacts = json.loads(archive.read(f"executions/{_EXECUTION}/artifacts.json"))
            summary = next(row["content"] for row in artifacts if row["artifact_type"] == "execution_summary")
            summary.pop("business_facts_ref")
            assert business_evidence(summary) == business_evidence(journal)
            assert business_evidence(
                _csv(archive, f"executions/{_EXECUTION}/trades.csv")[0]["trade_id"]
            ) == business_evidence("native-fill-01")
    finally:
        bundle.close()


def test_failed_terminal_transaction_keeps_original_worker_journal(facts_database, monkeypatch):
    """引用已生成后提交失败，完整 worker 证据必须随同终态写入一起回滚。"""
    _isolate_analysis_projection(monkeypatch)
    payload = _summary_result()
    journal = _pending_summary(payload)
    asyncio.run(_seed_pending_summary(facts_database, journal))

    compact = records.compact_execution_summary

    async def fail_after_compaction(session, record, *, result):
        await compact(session, record, result=result)
        await session.flush()
        artifact = await session.scalar(
            select(ExecutionArtifact).where(
                ExecutionArtifact.execution_id == _EXECUTION,
                ExecutionArtifact.artifact_type == ExecutionArtifactType.EXECUTION_SUMMARY,
            )
        )
        assert business_evidence(artifact.content["business_facts_ref"]["view"]) == business_evidence("result")
        stored_symbol = artifact.content.get("symbol_results", {}).get("rb2610", {})
        assert "orders" not in stored_symbol and "trades" not in stored_symbol
        raise RuntimeError("synthetic post-compaction persistence failure")

    monkeypatch.setattr(records, "compact_execution_summary", fail_after_compaction)
    with pytest.raises(RuntimeError, match="post-compaction persistence failure"):
        asyncio.run(_write_real_terminal(facts_database, payload))

    async def read_after_rollback():
        async with facts_database() as session:
            count = await session.scalar(select(func.count()).select_from(ExecuteRecord))
            artifact = await session.scalar(
                select(ExecutionArtifact).where(
                    ExecutionArtifact.execution_id == _EXECUTION,
                    ExecutionArtifact.artifact_type == ExecutionArtifactType.EXECUTION_SUMMARY,
                )
            )
            return count, artifact.schema_version, artifact.content

    count, version, retained = asyncio.run(read_after_rollback())
    assert business_evidence(count) == business_evidence(0)
    assert business_evidence(version) == business_evidence(2)
    assert business_evidence(retained) == business_evidence(journal)
    snapshot = asyncio.run(_collect(facts_database))
    assert snapshot.executions[_EXECUTION].record is None
    assert business_evidence(snapshot.executions[_EXECUTION].artifacts[0]["content"]) == business_evidence(journal)


def test_terminal_compaction_keeps_distinct_journal_observation(facts_database, monkeypatch):
    """不同的历史观测不能被最终结果引用覆盖。"""
    _isolate_analysis_projection(monkeypatch)
    payload = _summary_result()
    journal = _pending_summary(payload)
    journal["symbol_results"]["rb2610"]["orders"][0]["filled_volume"] = 0
    asyncio.run(_seed_pending_summary(facts_database, journal))
    asyncio.run(_write_real_terminal(facts_database, payload))

    async def read_summary():
        async with facts_database() as session:
            artifact = await session.scalar(
                select(ExecutionArtifact).where(
                    ExecutionArtifact.execution_id == _EXECUTION,
                    ExecutionArtifact.artifact_type == ExecutionArtifactType.EXECUTION_SUMMARY,
                )
            )
            return artifact.content, await resolve_artifact_content(session, artifact, account_id=1)

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


async def _seed_supplement_step(factory):
    async with factory() as session:
        session.add(
            SupplementGroup(
                id="facts-notification-group",
                account_id=1,
                fingerprint="facts-notification-fingerprint",
                base_scheduled_at="2026-10-20T09:00:00+08:00",
                expires_at="2026-10-20T23:00:00+08:00",
                configured_count=1,
                effective_count=1,
            )
        )
        await session.flush()
        session.add(
            SupplementStep(
                id="facts-notification-step",
                group_id="facts-notification-group",
                index=1,
                scheduled_at="2026-10-20T09:00:00+08:00",
                status="running",
                execution_id=_EXECUTION,
            )
        )
        await session.commit()


async def _stored_captured_notification(session):
    extension = fact_repository.extensions.execution_extension
    record = await session.scalar(select(ExecuteRecord).where(ExecuteRecord.execution_id == _EXECUTION))
    extra = await session.scalar(select(extension.c.extra).where(extension.c.record_id == record.id))
    return extra["server_notification_context"]


@pytest.mark.parametrize("existing_event", [False, True])
def test_finished_outbox_owns_large_context_and_preview_keeps_frozen_snapshot(
    facts_database, monkeypatch, existing_event
):
    """outbox 冻结成功后只留引用；已有通知正文与预览优先级保持不变。"""
    captured = _large_captured_notification()
    payload = _result()
    payload["extra"]["server_notification_context"] = deepcopy(captured)
    original_event = deepcopy(captured)
    original_event.update(event_id=f"execution:{_EXECUTION}", event_type="execution.finished", is_test=False)
    original_event["execution"].update(id=_EXECUTION, notified_at=_CREATED, is_test=False)
    asyncio.run(_store(facts_database, result=payload, event_context=original_event if existing_event else None))
    asyncio.run(_seed_supplement_step(facts_database))

    async def finish():
        async with facts_database() as session:
            intent = await session.scalar(select(ExecutionIntent).where(ExecutionIntent.execution_id == _EXECUTION))
            await supplement_notifications.finish_supplement_execution(session, intent)
            await session.commit()
        async with facts_database() as session:
            stored = await _stored_captured_notification(session)
            event = await session.scalar(
                select(NotificationEvent).where(NotificationEvent.id == f"execution:{_EXECUTION}")
            )
            record = await session.scalar(select(ExecuteRecord).where(ExecuteRecord.execution_id == _EXECUTION))
            detail = await read_execution_detail(session, record)
            return stored, event.context, detail

    stored, event_context, detail = asyncio.run(finish())
    assert len(json.dumps(stored)) < len(json.dumps(captured)) // 2
    assert not {"positions", "orders", "trades"}.intersection(stored)
    assert business_evidence(detail["result"]["extra"]["server_notification_context"]) == business_evidence(captured)
    if existing_event:
        assert business_evidence(event_context) == business_evidence(original_event)
    for key in ("account", "assets", "targets", "positions", "orders", "trades"):
        assert business_evidence(event_context[key]) == business_evidence(captured[key])

    forbidden_detail = AsyncMock(side_effect=AssertionError("frozen outbox must be selected before canonical detail"))
    monkeypatch.setattr(account_feishu, "run_in_threadpool", forbidden_detail)

    async def preview():
        async with facts_database() as session:
            context = await account_feishu._load_notification_preview_context(session, build_account(portfolio_id=None))
            assert business_evidence(context["account"]["name"]) == business_evidence("frozen-at-execution")
            assert business_evidence(context["orders"]) == business_evidence(captured["orders"])
            context["orders"][0]["order_id"] = "preview-only-edit"
            await session.flush()
            event = await session.scalar(
                select(NotificationEvent).where(NotificationEvent.id == f"execution:{_EXECUTION}")
            )
            return event.context

    assert business_evidence(asyncio.run(preview())) == business_evidence(event_context)
    forbidden_detail.assert_not_awaited()


def test_context_compaction_rollback_retains_only_copy_when_outbox_is_not_committed(facts_database):
    """通知及去重提交失败时，捕获正文、步骤状态和 outbox 一起回滚。"""
    captured = _large_captured_notification()
    payload = _result()
    payload["extra"]["server_notification_context"] = deepcopy(captured)
    asyncio.run(_store(facts_database, result=payload))
    asyncio.run(_seed_supplement_step(facts_database))

    async def fail_after_compaction():
        async with facts_database() as session:
            intent = await session.scalar(select(ExecutionIntent).where(ExecutionIntent.execution_id == _EXECUTION))
            await supplement_notifications.finish_supplement_execution(session, intent)
            await session.flush()
            assert len(json.dumps(await _stored_captured_notification(session))) < len(json.dumps(captured)) // 2
            raise RuntimeError("synthetic notification transaction failure")

    with pytest.raises(RuntimeError, match="notification transaction failure"):
        asyncio.run(fail_after_compaction())

    async def inspect_after_rollback():
        async with facts_database() as session:
            event = await session.scalar(
                select(NotificationEvent).where(NotificationEvent.id == f"execution:{_EXECUTION}")
            )
            step = await session.get(SupplementStep, "facts-notification-step")
            return event, step.status, await _stored_captured_notification(session)

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
        async with facts_database() as session:
            intent = await session.scalar(select(ExecutionIntent).where(ExecutionIntent.execution_id == _EXECUTION))
            await supplement_notifications.finish_supplement_execution(session, intent)
            await session.commit()
        async with facts_database() as session:
            event = await session.scalar(
                select(NotificationEvent).where(NotificationEvent.id == f"execution:{_EXECUTION}")
            )
            return await _stored_captured_notification(session), event.context

    stored, retained_outbox = asyncio.run(finish())
    assert business_evidence(stored) == business_evidence(captured)
    assert business_evidence(retained_outbox) == business_evidence(outbox)
    forbidden_detail = AsyncMock(side_effect=AssertionError("existing outbox must win preview"))
    monkeypatch.setattr(account_feishu, "run_in_threadpool", forbidden_detail)

    async def preview():
        async with facts_database() as session:
            return await account_feishu._load_notification_preview_context(session, build_account(portfolio_id=None))

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
        async with facts_database() as session:
            intent = await session.scalar(select(ExecutionIntent).where(ExecutionIntent.execution_id == _EXECUTION))
            await supplement_notifications.finish_supplement_execution(session, intent)
            await session.commit()
        async with facts_database() as session:
            event = await session.scalar(
                select(NotificationEvent).where(NotificationEvent.id == f"execution:{_EXECUTION}")
            )
            stored = await _stored_captured_notification(session)
            preview = await account_feishu._load_notification_preview_context(session, build_account(portfolio_id=None))
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
    """捕获正文移到 outbox 后，其大列仍需在解码和冷重建前计入预算。"""
    captured = _large_captured_notification()
    for key in ("positions", "orders", "trades"):
        captured[key] *= 8
    payload = _result()
    payload["extra"]["server_notification_context"] = captured
    asyncio.run(_store(facts_database, result=payload))
    asyncio.run(_seed_supplement_step(facts_database))

    async def finish_and_optionally_corrupt():
        async with facts_database() as session:
            intent = await session.scalar(select(ExecutionIntent).where(ExecutionIntent.execution_id == _EXECUTION))
            await supplement_notifications.finish_supplement_execution(session, intent)
            await session.commit()
        async with facts_database() as session:
            stored = await _stored_captured_notification(session)
            assert not {"positions", "orders", "trades"}.intersection(stored)
            assert len(json.dumps(stored)) < 21000
            raw_size = await session.scalar(
                text("SELECT length(CAST(context AS BLOB)) FROM notification_event WHERE id=:event_id"),
                {"event_id": f"execution:{_EXECUTION}"},
            )
            assert raw_size > 64000
        if malformed:
            async with facts_database() as session:
                await session.execute(
                    text("UPDATE notification_event SET context=:invalid WHERE id=:event_id"),
                    {"invalid": "malformed-notification-context" * 10000, "event_id": f"execution:{_EXECUTION}"},
                )
                await session.commit()

    asyncio.run(finish_and_optionally_corrupt())
    cold_reader = AsyncMock(side_effect=AssertionError("outbox budget preflight must precede canonical hydration"))
    monkeypatch.setattr(fact_repository, "read_execution_detail", cold_reader)
    budget = ExportBudget(limits=replace(EvidenceLimits(), bytes=64000))

    with pytest.raises(EvidenceError) as error:
        asyncio.run(_collect(facts_database, budget))

    assert business_evidence(error.value.code) == business_evidence("EVIDENCE_TOO_LARGE")
    assert business_evidence(error.value.status) == business_evidence(413)
    cold_reader.assert_not_awaited()
