"""检查恢复或无关委托不能制造通过；网络由公共 fixture 禁止。"""

import json

import pytest
from test_scenarios import make_runner

from ft_ctp.api import ApiError
from ft_ctp.preconditions import unknown_contract
from ft_ctp.runner import Conditions
from ft_ctp.scenarios import pause


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("none", "passed"),
        ("replace", "failed"),
        ("replace_after_normal", "failed"),
        ("missing_instance", "unknown"),
        ("incomplete", "unknown"),
        ("closed", "failed"),
        ("wrong_code", "insufficient_conditions"),
        ("wrong_http", "insufficient_conditions"),
        ("normal_error", "error"),
    ],
)
def test_negative_probe_requires_original_session_and_normal_query(tmp_path, fault, expected):
    runner, api = make_runner(tmp_path)
    original, phase, recoveries = api.request, 0, []

    def request(method, path, body=None, **kwargs):
        nonlocal phase
        if path.endswith("/reconnect"):
            recoveries.append(path)
        if path == "/instrument-catalog/query":
            if body["symbols"] == [runner.config.unknown_symbol]:
                phase = 1
                if fault == "replace":
                    api.instance_id = "replacement"
                code = "CATALOG.TIMEOUT" if fault == "wrong_code" else "CATALOG.NOT_FOUND"
                raise ApiError(
                    json.dumps({"detail": {"code": code, "causes": [{"message": "original"}]}}),
                    503 if fault == "wrong_http" else 404,
                )
            phase = 2
            if fault == "normal_error":
                raise ApiError("normal query failed", 503)
            if fault == "replace_after_normal":
                api.instance_id = "replacement"
        value = original(method, path, body, **kwargs)
        if phase and path.endswith("/connection") and fault == "closed":
            return {"status": "disconnected", "worker_alive": False}
        if phase and "/orders?" in path:
            if fault == "missing_instance":
                value.pop("instance_id")
            if fault == "incomplete":
                value["complete"] = False
        return value

    api.request = request
    if expected == "error":
        with pytest.raises(ApiError, match="normal query failed"):
            unknown_contract(runner, "probe")
    else:
        row = unknown_contract(runner, "probe")
        assert row["status"] == expected
    assert not recoveries and not api.sessions
    raw = json.loads((runner.bundle.path / "probe/unknown-contract.json").read_text())
    assert raw["detail"]["causes"] == [{"message": "original"}]
    assert (runner.bundle.path / "probe/connection-after-probe.json").exists()
    if expected == "passed":
        assert phase == 2
        assert (runner.bundle.path / "probe/normal-catalog-query.json").exists()
        assert (runner.bundle.path / "probe/session-after-normal-query.json").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "delayed",
        "missing_native",
        "wrong_execution",
        "wrong_order",
        "wrong_symbol",
        "uncertain",
        "filled",
        "incomplete",
        "late",
        "extra_order",
    ],
)
def test_after_send_needs_owned_native_pending_before_terminate(tmp_path, fault):
    runner, api = make_runner(tmp_path, pause_checkpoint="after_send", terminate_mode="cancel_pending")
    runner.preflight()
    runner.prepare()
    original, reads, receipts = api.request, 0, []

    def request(method, path, body=None, **kwargs):
        nonlocal reads
        if path.endswith("/terminate"):
            receipts.append(body)
            assert (runner.bundle.path / "pause/confirmed-pending-order.json").exists()
        value = original(method, path, body, **kwargs)
        if "/orders?" in path and api.sessions:
            reads += 1
            if fault == "late":
                runner.clock.sleep(runner.config.timeout_seconds)
            mutate_pending_snapshot(value, fault, reads)
        return value

    api.request = request
    if fault in {"none", "delayed"}:
        assert pause(runner, "pause")["status"] == "passed"
        assert len(receipts) == 1 and receipts[0]["mode"] == "cancel_pending"
        sample = json.loads((runner.bundle.path / "pause/confirmed-pending-order.json").read_text())
        assert sample["matched"][0]["native_identity"]["order_sys_id"]
        if fault == "delayed":
            assert reads >= 2
    else:
        with pytest.raises(Conditions, match="未确认本次 execution"):
            pause(runner, "pause")
        assert not receipts
        assert (runner.bundle.path / "pause/pending-order-samples.json").exists()


def test_real_algorithm_anchor_links_returned_order(tmp_path):
    runner, api = make_runner(tmp_path)
    runner.preflight()
    runner.prepare()
    row = runner.trade("open", "open")
    assert row["status"] == "passed"
    execution_id = row["execution_id"]
    anchor = next(e for e in api.events[execution_id] if e.get("reason_code") == "FT.CHECKPOINT.after_send")
    assert anchor["details"]["order_id"] in api.sessions[execution_id].orders


def mutate_pending_snapshot(value, fault, reads):
    rows = value["data"]
    if rows:
        row = rows[0]
        if fault == "missing_native" or fault == "delayed" and reads == 1:
            row["native_identity"]["order_sys_id"] = ""
        if fault == "wrong_execution":
            row["execution_id"] = "unrelated"
        if fault == "wrong_order":
            row["order_id"] = "unrelated"
        if fault == "wrong_symbol":
            row["symbol"] = "OTHER"
        if fault == "uncertain":
            row["uncertain"] = True
        if fault == "filled":
            row.update(status="已成交", remaining_volume=0)
        if fault == "extra_order":
            rows.append({**row, "order_id": "unrelated"})
    if fault == "incomplete":
        value["complete"] = False
