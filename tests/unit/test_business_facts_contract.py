"""业务证据只规范已结构化时间字段，原生 JSON 保留原文。"""

from scripts.business_facts_contract import business_evidence


def test_scalar_time_aliases_match_without_changing_native_json():
    native = {"trade_time": "2026-01-01T09:00:00+08:00", "update_time": "native"}
    source = {
        "orders": [{"create_time": "2026-01-01T09:00:00.123456+08:00", "extra": native}],
        "trades": [{"trade_time": "2026-01-01T01:00:00.123456Z", "extra": native}],
        "account_assets": {"update_time": "unknown-native", "extra": native},
        "first_ticks": {"A": {"update_time": "2026-01-01T09:00:00.123456+08:00", "extra": native}},
    }
    expected = {
        **source,
        "orders": [{"create_time": "2026-01-01T01:00:00.123456Z", "extra": native}],
        "first_ticks": {"A": {"update_time": "2026-01-01T01:00:00.123456Z", "extra": native}},
    }
    assert business_evidence(source) == business_evidence(expected)
    assert business_evidence(source)["orders"][0]["extra"] == native
    assert source["orders"][0]["create_time"].endswith("+08:00")
    changed_native = {**expected, "orders": [{**expected["orders"][0], "extra": {**native, "trade_time": "changed"}}]}
    assert business_evidence(source) != business_evidence(changed_native)


def test_time_name_in_unrecognized_json_is_not_normalized():
    value = {"extension": {"update_time": "2026-01-01T09:00:00+08:00"}}
    assert business_evidence(value) == value
