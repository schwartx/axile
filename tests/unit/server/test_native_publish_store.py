"""Native publication exchanges a small identity-checked receipt, never result rows."""

import asyncio
import json
import sys

import pytest

from axile.server.native_analysis import NativeAnalysis, NativeAnalysisError, NativeAnalysisProtocolError


def executable(tmp_path, output, *, error=None):
    path = tmp_path / "publisher"
    body = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "request = json.load(sys.stdin)\n"
        "assert request['command'] == 'publish_store' and request['protocol'] == 1\n"
        "assert request['snapshot_id'] == request['claim_token'] == 'owned'\n"
        "assert request['account_id'] == 7 and request['expected_source_version'] == 4\n"
        "assert set(request) == {'command','protocol','account_id','expected_source_version','snapshot_id',"
        "'claim_token','logic_version','engine_version','computed_at','settings','trade_channel'}\n"
        f"sys.stdout.buffer.write({output!r})\n"
    )
    if error:
        body += f"sys.stderr.write({json.dumps(error)!r})\nsys.exit(1)\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def receipt(**changes):
    return {
        "protocol": 1,
        "command": "publish_store",
        "published": True,
        "snapshot_id": "owned",
        "account_id": 7,
        "source_version": 4,
        "data_until": None,
        "reason": None,
        **changes,
    }


def invoke(tmp_path, output, *, error=None):
    native = NativeAnalysis(executable(tmp_path, output, error=error), tmp_path / "db", timeout=2)
    return asyncio.run(
        native.publish_store(
            account_id=7,
            expected_source_version=4,
            snapshot_id="owned",
            logic_version="18",
            engine_version="equity-ledger-1",
            computed_at="2026-10-20T12:00:00+08:00",
            settings={"backtest_weight_type": "cs", "backtest_fee_rate": 0.0002},
            trade_channel="ctp",
        )
    )


@pytest.mark.parametrize("reason", [None, "source_changed", "claim_lost", "account_missing"])
def test_publish_store_only_passes_small_control_request_and_receipt(tmp_path, reason):
    expected = receipt(published=reason is None, reason=reason, row_counts={"performance_result_range": 3})
    assert invoke(tmp_path, json.dumps(expected).encode() + b"\n") == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol": True},
        {"command": "publish"},
        {"published": 1},
        {"snapshot_id": "another"},
        {"account_id": True},
        {"account_id": 8},
        {"source_version": True},
        {"source_version": 5},
        {"data_until": []},
        {"reason": "source_changed"},
        {"published": False},
        {"published": False, "reason": "unknown"},
        {"row_counts": {"table": True}},
        {"row_counts": {"table": -1}},
        {"row_counts": []},
        {"rows": [[1, 2, 3]]},
    ],
)
def test_receipt_must_match_identity_types_and_publish_outcome(tmp_path, changes):
    with pytest.raises(NativeAnalysisProtocolError) as error:
        invoke(tmp_path, json.dumps(receipt(**changes)).encode())
    assert error.value.code == "NATIVE_INVALID_RECEIPT"


@pytest.mark.parametrize("output", [b"{}", b"[]", b"not JSON", b"\xff", b'{"row_counts":{"table":NaN}}'])
def test_receipt_rejects_invalid_json_or_missing_contract_fields(tmp_path, output):
    with pytest.raises(NativeAnalysisProtocolError) as error:
        invoke(tmp_path, output)
    assert error.value.code == "NATIVE_INVALID_RECEIPT"


def test_receipt_budget_is_smaller_than_legacy_result_budget(tmp_path):
    with pytest.raises(NativeAnalysisProtocolError) as error:
        invoke(tmp_path, b"x" * (70 * 1024))
    assert error.value.code == "NATIVE_OUTPUT_LIMIT"


def test_success_looking_receipt_cannot_override_process_failure(tmp_path):
    with pytest.raises(NativeAnalysisError) as error:
        invoke(tmp_path, json.dumps(receipt()).encode(), error={"code": "DATABASE_WRITE", "error": "write failed"})
    assert error.value.code == "DATABASE_WRITE"
