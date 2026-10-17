"""负查询与终止动作的前置证据，不以恢复后的成功掩盖原会话失败。"""

import json

from axile.executor.algorithms.utils.clock import clock_now_iso
from axile.executor.constants.order_status import OrderStatus

from ft_ctp.api import ApiError
from ft_ctp.checks import day_counts
from ft_ctp.runner import TERMINAL, Conditions


def session_sample(runner, step, name):
    """委托快照实例包含 worker 身份和订单簿代次，前后必须保持一致。"""
    frame = {"at": clock_now_iso(), "connection": runner.call("GET", runner.base + "/connection")}
    runner.bundle.write(f"{step}/{name}.json", frame)
    frame["orders"] = runner.call("GET", runner.base + "/orders?refresh=true")
    runner.bundle.write(f"{step}/{name}.json", frame)
    return frame


def session_health(before, after):
    """缺少身份或完整快照时保留未知；失联或实例变化不能记通过。"""
    for frame in (before, after):
        if frame["connection"].get("status") != "ready" or not frame["connection"].get("worker_alive"):
            return "failed", "负查询前后原会话未保持 ready/alive"
        if not frame["orders"].get("complete") or not frame["orders"].get("instance_id"):
            return "unknown", "缺少完整委托快照或会话实例身份"
    if before["orders"]["instance_id"] != after["orders"]["instance_id"]:
        return "failed", "负查询后会话实例或订单簿代次发生变化"
    return None


def unknown_contract(runner, step):
    """验证结构化负查询、原实例和后续正常查询，不在探测内重连。"""
    before = runner.call("GET", runner.base + "/control/monitor")
    original = session_sample(runner, step, "session-before-probe")
    error, status, detail = None, None, {}
    try:
        runner.call(
            "POST",
            "/instrument-catalog/query",
            {
                "account_id": runner.account_id,
                "symbols": [runner.config.unknown_symbol],
                "require_specs": True,
                "use_cache": False,
            },
        )
    except ApiError as exc:
        error, status = str(exc), exc.status
        try:
            payload = json.loads(error)
            detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
        except (ValueError, TypeError):
            pass
    runner.bundle.write(f"{step}/unknown-contract.json", {"error": error, "http_status": status, "detail": detail})
    after_probe = session_sample(runner, step, "connection-after-probe")
    verdict = session_health(original, after_probe)
    if verdict is None:
        runner.call("GET", runner.base)
        result = runner.call(
            "POST",
            "/instrument-catalog/query",
            {
                "account_id": runner.account_id,
                "symbols": [runner.config.symbol],
                "require_specs": True,
                "use_cache": False,
            },
        )
        runner.bundle.write(f"{step}/normal-catalog-query.json", result)
        verdict = session_health(original, session_sample(runner, step, "session-after-normal-query"))
        if not result.get("data"):
            verdict = ("unknown", "后续正常合约查询未返回资料")
    after = runner.call("GET", runner.base + "/control/monitor")
    runner.bundle.write(f"{step}/probe-counters.json", {"before": before, "after": after})
    if verdict is None and not (
        status == 404 and isinstance(detail, dict) and detail.get("code") == "CATALOG.NOT_FOUND"
    ):
        verdict = ("insufficient_conditions", "未命中结构化 404/CATALOG.NOT_FOUND")
    if verdict is None and (day_counts(before) is None or day_counts(before) != day_counts(after)):
        verdict = ("unknown", "负查询前后报撤计数不可核对或发生变化")
    verdict = verdict or ("passed", "结构化未知合约拒绝后，原会话及正常查询保持可用，报撤计数不变")
    return {"scenario": step, "status": verdict[0], "reason": verdict[1]}


def pending_matches(runner, step, events, snapshot):
    """同时核对本批次发送锚点、execution 归属和真实交易所订单身份。"""
    owned = {
        event.get("details", {}).get("order_id")
        for event in events.get("data", [])
        if event.get("reason_code") == "FT.CHECKPOINT.after_send"
        and event.get("details", {}).get("batch") == runner.bundle.index["batch"]
        and event.get("details", {}).get("step") == step
    }
    return [
        row
        for row in snapshot.get("data", [])
        if row.get("order_id")
        and row["order_id"] in owned
        and row.get("execution_id") == runner.active_execution
        and row.get("symbol") == runner.config.symbol
        and row.get("status") in OrderStatus.ACTIVE_STATUSES
        and not row.get("uncertain", False)
        and row.get("remaining_volume", 0) > 0
        and row.get("native_identity", {}).get("exchange_id", "").strip()
        and row.get("native_identity", {}).get("order_sys_id", "").strip()
    ]


def wait_owned_pending(runner, step):
    """未确认本次真实挂单时不发送场景终止；外层仍按既有规则安全收尾。"""
    deadline, samples = runner.clock.time() + runner.config.timeout_seconds, []
    while runner.clock.time() < deadline:
        events = runner.api.pages(f"/account/executions/{runner.active_execution}/events")
        orders = runner.call("GET", runner.base + "/orders?refresh=true")
        state = runner.call("GET", f"/account/executions/{runner.active_execution}")
        matches = pending_matches(runner, step, events, orders)
        sample = {"at": clock_now_iso(), "events": events, "orders": orders, "execution": state, "matched": matches}
        samples.append(sample)
        runner.bundle.write(f"{step}/pending-order-samples.json", samples)
        if (
            runner.clock.time() < deadline
            and events.get("complete")
            and orders.get("complete")
            and state.get("status") == "RUNNING"
            and len(matches) == 1
            and len(orders.get("data", [])) == 1
        ):
            runner.bundle.write(f"{step}/confirmed-pending-order.json", sample)
            return
        if state.get("status") in TERMINAL:
            break
        runner.clock.sleep(min(runner.config.poll_seconds, max(0, deadline - runner.clock.time())))
    raise Conditions("未确认本次 execution 的真实原生挂单身份，未发送场景终止操作；收尾另行记录")
