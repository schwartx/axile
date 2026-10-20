"""动态扩展只拥有自身证据，异常历史根值具有显式状态。"""

import json

import pytest

from axile.server.business_facts.extensions import (
    DOMAIN_FIELDS,
    decode_extensions,
    decode_result_root_state,
    encode_extensions,
    encode_result_root_state,
    execution_extension,
)


def _rows(batch):
    return {table.name: values for table, values in batch.items()}


def test_result_extensions_exclude_all_owned_business_domains():
    payload = {name: {"must_not_duplicate": name} for name in DOMAIN_FIELDS}
    payload.update({"memory": {"diagnostic": [1, None]}, "extra": {"channel": "custom"}, "unknown": False})
    batch = encode_extensions(3, payload)
    row = batch[execution_extension][0]

    assert row["root_kind"] == "object"
    assert row["root_value"] is None
    assert "must_not_duplicate" not in json.dumps(row)
    assert row["audit_evidence"]["unrecognized_fields"] == {"unknown": False}
    assert decode_extensions(_rows(batch)) == {
        "memory": {"diagnostic": [1, None]},
        "extra": {"channel": "custom"},
        "audit_evidence": {"unrecognized_fields": {"unknown": False}},
    }
    assert decode_result_root_state(_rows(batch)) == ("object", None)


@pytest.mark.parametrize(
    ("payload", "kind"),
    [(None, "null"), ([], "empty_list"), (0, "scalar"), (False, "scalar"), ("", "scalar"), (2.5, "scalar")],
)
def test_historical_non_object_root_does_not_become_empty_object(payload, kind):
    batch = encode_result_root_state(4, payload)
    row = batch[execution_extension][0]
    assert row["root_kind"] == kind
    assert decode_result_root_state(_rows(batch)) == (kind, payload)
    assert decode_extensions(_rows(batch)) == {}


def test_historical_scalar_nonfinite_root_is_json_safe():
    batch = encode_result_root_state(4, float("nan"))
    json.dumps(batch[execution_extension], allow_nan=False)
    kind, value = decode_result_root_state(_rows(batch))
    assert kind == "scalar"
    assert value == {"invalid_number": "nan"}


@pytest.mark.parametrize("payload", [[{"orders": []}], [1], object()])
def test_unmappable_root_fails_before_hiding_a_whole_document(payload):
    with pytest.raises(ValueError, match="Unsupported historical result root"):
        encode_result_root_state(4, payload)


def test_missing_new_root_row_is_visible_corruption():
    with pytest.raises(ValueError, match="exactly one record"):
        decode_result_root_state({})


def test_empty_mapping_and_no_extensions_are_still_object():
    batch = encode_result_root_state(5, {})
    assert decode_result_root_state(_rows(batch)) == ("object", None)
    assert decode_extensions(_rows(batch)) == {}


@pytest.mark.parametrize(("kind", "value"), [("null", 4), ("empty_list", [1]), ("scalar", {})])
def test_root_state_cannot_silently_disagree_with_value(kind, value):
    batch = encode_result_root_state(5, value if kind != "empty_list" else [])
    row = batch[execution_extension][0]
    row["root_kind"] = kind
    row["root_value"] = value
    row["audit_evidence"] = {}
    with pytest.raises(ValueError, match="disagrees with its state"):
        decode_result_root_state(_rows(batch))
