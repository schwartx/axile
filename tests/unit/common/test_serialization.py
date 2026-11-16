"""内部对象直接流转，配置入口校验及公开出口裁剪的行为契约。"""

import json
from dataclasses import dataclass, field

import pytest

from axile.common.serialization import copy_data, from_payload, parameter_schema, parse_input, to_payload
from axile.executor.algorithms.defaults.single_maker.impl import SingleMakerParams
from axile.executor.models.unified_price import UnifiedPriceData
from axile.server.db.models.account import AccountUpdate


def test_runtime_construction_and_copy_do_not_validate(monkeypatch):
    def reject(_self):
        raise AssertionError("内部流转不应重复校验")

    monkeypatch.setattr(SingleMakerParams, "validate", reject)
    params = SingleMakerParams(max_wait_seconds=60)
    assert copy_data(params, update={"max_wait_seconds": 30}).max_wait_seconds == 30
    assert from_payload(SingleMakerParams, params) is params


def test_edit_entry_preserves_bounds_and_combined_chase_rules():
    with pytest.raises(ValueError, match="3600"):
        parse_input(SingleMakerParams, {"max_wait_seconds": 4000})
    with pytest.raises(ValueError, match="速率限制"):
        parse_input(SingleMakerParams, {"chase_enabled": True, "max_chase_count": 20, "chase_interval": 0.5})
    assert parse_input(SingleMakerParams, {"max_wait_seconds": 30}).max_wait_seconds == 30


def test_patch_keeps_omission_null_and_false_distinct():
    assert to_payload(parse_input(AccountUpdate, {}), exclude_unset=True) == {}
    raw = {"feishu_key": None, "replay_recording_enabled": False, "algorithm_schedule": []}
    assert to_payload(parse_input(AccountUpdate, raw), exclude_unset=True) == raw
    assert to_payload(AccountUpdate(feishu_key=None), exclude_unset=True) == {"feishu_key": None}


def test_price_roundtrip_keeps_computed_fields_and_snapshot_isolation():
    price = UnifiedPriceData(
        symbol="rb2610", last_price=100, bids=[(99, 2)], asks=[(101, 3)], volume=0, timestamp=1, update_time=""
    )
    payload = to_payload(price, mode="json")
    assert payload["book_valid"] is True
    restored = from_payload(UnifiedPriceData, json.loads(json.dumps(payload)))
    assert restored.bids == price.bids
    copied = copy_data(restored, deep=True)
    copied.bids[0] = (98, 1)
    assert restored.bids == [(99, 2)]


def test_nested_public_projection_never_exports_internal_credentials():
    @dataclass
    class Snapshot:
        inputs: dict
        symbol_results: dict
        private: str = field(default="internal", metadata={"exclude": True})

    snapshot = Snapshot(
        inputs={"account_config": {"password": "secret"}, "algorithm": "TWAP"},
        symbol_results={"rb2610": {"orders": [{"order_id": "1", "extra": {"raw": "private"}}]}},
    )
    exported = to_payload(
        snapshot,
        mode="json",
        exclude={"inputs": {"account_config"}, "symbol_results": {"__all__": {"orders": {"__all__": {"extra"}}}}},
    )
    assert exported == {"inputs": {"algorithm": "TWAP"}, "symbol_results": {"rb2610": {"orders": [{"order_id": "1"}]}}}
    assert snapshot.inputs["account_config"]["password"] == "secret"


def test_json_and_algorithm_form_keep_existing_values():
    assert to_payload({"missing": float("nan"), "overflow": float("inf")}, mode="json") == {
        "missing": None,
        "overflow": None,
    }
    fields = parameter_schema(SingleMakerParams)["properties"]
    assert fields["max_wait_seconds"]["minimum"] == 1
    assert fields["max_wait_seconds"]["maximum"] == 3600
    assert fields["max_wait_seconds"]["default"] == 60
    assert fields["max_wait_seconds"]["x-unit"] == "秒"
