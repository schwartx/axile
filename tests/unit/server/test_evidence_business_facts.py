"""结构化执行业务事实的归档来源与快照语义。"""

import asyncio
import json
from zipfile import ZipFile

import pytest

from axile.server.business_facts import artifacts as fact_artifacts
from axile.server.evidence.archive import build_archive
from axile.server.evidence.models import (
    EvidenceError,
    EvidenceProfile,
    EvidenceSnapshot,
    ExecutionEvidence,
    ExportBudget,
)
from axile.server.evidence.report import execution_report, result_payload


def test_canonical_result_preserves_native_occurrences_and_truthful_source():
    item = ExecutionEvidence(
        key="typed-1",
        execution_id="typed-1",
        record={
            "id": 7,
            "input": {"curr_target": {}},
            "result": {
                "status": "PARTIAL",
                "symbol_results": {
                    "rb/2610": {
                        "trades": [
                            {"trade_id": "same", "trade_price": 0, "extra": {"TradingDay": "20261015"}},
                            {"trade_id": "same", "trade_price": 1, "extra": {"TradingDay": "20261015"}},
                            {"trade_id": "same", "trade_price": 0, "extra": {"TradingDay": "20261016"}},
                            {"trade_id": None, "trade_price": 0},
                        ]
                    },
                    "hc2610": {"trades": [{"trade_id": "same", "trade_price": 0}]},
                },
            },
        },
    )
    report, _, trades, _ = execution_report(item)
    assert report["result_source"] == "record.json#/result"
    assert report["trades"] == 4
    assert report["trade_snapshots"] == 5
    assert report["unidentified_trade_snapshots"] == 1
    assert trades[0]["source_path"] == "record.json#/result/symbol_results/rb~12610/trades/0"
    assert [row["trade_price"] for row in trades] == [0, 1, 0, 0, 0]


def test_empty_canonical_result_does_not_fall_back_to_summary():
    item = ExecutionEvidence(
        key="empty",
        execution_id="empty",
        record={"result": {}},
        artifacts=[{"id": 1, "artifact_type": "execution_summary", "content": {"status": "SUCCESS"}}],
    )
    assert result_payload(item) == ({}, "record.json#/result")


@pytest.mark.parametrize("result", [None, [], "malformed"])
def test_explicit_historical_result_root_is_not_replaced_with_another_evidence(result):
    item = ExecutionEvidence(key="broken", execution_id="broken", record={"result": result})
    assert result_payload(item) == ({}, "record.json#/result")
    assert item.record["result"] is result


def test_missing_canonical_result_is_visible_instead_of_using_another_evidence():
    item = ExecutionEvidence(key="broken", execution_id="broken", record={})
    with pytest.raises(EvidenceError, match="事实不完整") as error:
        result_payload(item)
    assert error.value.code == "EVIDENCE_INVALID_DATA"


def test_summary_reconciliation_points_to_exact_persisted_artifact_occurrence():
    item = ExecutionEvidence(
        key="summary-only",
        execution_id="summary-only",
        artifacts=[
            {"id": 8, "artifact_type": "environment_snapshot", "content": {}},
            {"id": 9, "artifact_type": "execution_summary", "content": {"reconciliation": {"symbols": []}}},
        ],
    )
    report, _, _, _ = execution_report(item)
    assert report["result_source"] == "artifacts.json#/1/content"
    assert report["reconciliation_source"] == "artifacts.json#/1/content/reconciliation"


def test_archive_marks_canonical_assembly_and_preserves_missing_asset_values():
    item = ExecutionEvidence(
        key="typed-1",
        execution_id="typed-1",
        record={"id": 7, "input": {}, "result": {"account_assets": {"positions": None, "total_asset": 0}}},
    )
    snapshot = EvidenceSnapshot(account={"id": 1, "name": "test", "channel": "ctp"}, executions={item.key: item})
    archive = build_archive(snapshot, EvidenceProfile.INTERNAL, ExportBudget())
    try:
        with ZipFile(archive.path) as source:
            manifest = json.loads(source.read("manifest.json"))
            record = json.loads(source.read("executions/typed-1/record.json"))
        assert manifest["schema_version"] == 3
        entry = next(row for row in manifest["files"] if row["path"].endswith("/record.json"))
        assert entry["source"] == "executerecord; canonical_business_facts_assembled_view"
        assert record["result"]["account_assets"] == {"positions": None, "total_asset": 0}
        assert "raw_result" not in record
    finally:
        archive.close()


def test_target_event_ref_only_emits_selected_fields_and_existing_diagnostics(monkeypatch):
    payload = {
        "curr_target": {"rb2610": 0},
        "last_target": {},
        "strategy_target": {"rb2610": 0.5},
        "account_target": {"rb2610": 0.25},
        "sizing_context": {"short_leverage": 3},
    }

    async def frozen_input(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(fact_artifacts, "_read_owned_input", frozen_input)
    reference = fact_artifacts.make_artifact_reference(
        view="targets", input_id="typed-input", select_fields=["curr_target"]
    )
    decision = {**reference, "reason_code": "frozen-native-code"}
    event = {"execution_id": "typed-1", "event_type": "target_computed", "details": {"decision": decision}}
    resolved = asyncio.run(fact_artifacts.resolve_event_details(None, event, account_id=1))
    public = {key: value for key, value in resolved["decision"].items() if key != "business_facts_ref"}
    assert public == {"reason_code": "frozen-native-code", "curr_target": {"rb2610": 0}}
    assert event["details"] == {"decision": decision}
    assert resolved["decision"]["business_facts_ref"]["select_fields"] == ["curr_target"]

    artifact = {
        "execution_id": "typed-1",
        "artifact_type": "target_snapshot",
        "content": fact_artifacts.make_artifact_reference(view="targets", input_id="typed-input"),
    }
    target = asyncio.run(fact_artifacts.resolve_artifact_content(None, artifact, account_id=1))
    assert {key: value for key, value in target.items() if key != "business_facts_ref"} == {
        key: value for key, value in payload.items() if key != "account_target"
    }


def test_target_event_missing_selected_field_is_not_inferred_from_another_target(monkeypatch):
    async def frozen_input(*_args, **_kwargs):
        return {"account_target": {"rb2610": 0.25}}

    monkeypatch.setattr(fact_artifacts, "_read_owned_input", frozen_input)
    event = {
        "execution_id": "typed-1",
        "event_type": "target_computed",
        "details": {
            "decision": fact_artifacts.make_artifact_reference(
                view="targets", input_id="typed-input", select_fields=["curr_target"]
            )
        },
    }
    with pytest.raises(fact_artifacts.BusinessFactReferenceError, match="选定字段缺失"):
        asyncio.run(fact_artifacts.resolve_event_details(None, event, account_id=1))


@pytest.mark.parametrize("selected", [[], None, ["account_config"], ["curr_target", "curr_target"], "curr_target"])
def test_reference_rejects_invalid_explicit_field_selector(selected):
    content = fact_artifacts.make_artifact_reference(view="targets", input_id="typed-input")
    content["business_facts_ref"]["select_fields"] = selected
    with pytest.raises(fact_artifacts.BusinessFactReferenceError, match="字段选择"):
        fact_artifacts.parse_artifact_reference(content)


def test_target_event_reference_requires_an_explicit_field_selector():
    event = {
        "execution_id": "typed-1",
        "event_type": "target_computed",
        "details": {"decision": fact_artifacts.make_artifact_reference(view="targets", input_id="typed-input")},
    }
    with pytest.raises(fact_artifacts.BusinessFactReferenceError):
        asyncio.run(fact_artifacts.resolve_event_details(None, event, account_id=1))
