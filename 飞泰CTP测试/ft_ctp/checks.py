"""按业务证据判定，任务成功、受理和列表消失均不是成交证明。"""

import math

from axile.executor.constants.order_status import OrderStatus


def find_results(value):
    """递归定位现有 execution summary 中的算法结果。"""
    results = []
    if isinstance(value, dict):
        if isinstance(value.get("memory"), dict) and "feitai" in value["memory"]:
            results.append(value)
        else:
            for child in value.values():
                results.extend(find_results(child))
    elif isinstance(value, list):
        for child in value:
            results.extend(find_results(child))
    return results


def find_core_results(value):
    """核心可在目标已到位时直接产生 NOOP，不调用用户算法。"""
    results = []
    if isinstance(value, dict):
        if value.get("algorithm") == "FT-CTP-SCENARIO" and "status" in value and "symbol" in value:
            results.append(value)
        else:
            for child in value.values():
                results.extend(find_core_results(child))
    elif isinstance(value, list):
        for child in value:
            results.extend(find_core_results(child))
    return results


def judge_core_noop(action, result, config, assets, pending, accounting):
    """重复目标需同时证明核心 NOOP、实际仓位到位和报撤增量为零。"""
    target = (1 if config.side == "long" else -1) * config.lots
    positions = [p for p in assets.get("positions", []) if p["symbol"] == config.symbol and p["volume"]]
    volume = sum((-1 if p["direction"] in {"空头", "SHORT"} else 1) * p["volume"] for p in positions)
    valid = (
        action == "open"
        and result.get("status") == "NOOP"
        and result.get("target_volume") == target
        and not result.get("orders")
        and not result.get("trades")
        and assets.get("source") == "real"
        and len({p["direction"] for p in positions}) == 1
        and volume == target
        and pending.get("complete")
        and not pending.get("data")
        and accounting.get("complete")
        and accounting.get("total") == 0
    )
    return (
        ("passed", "核心 NOOP、实际目标持仓与零报撤增量一致，重复执行未追加订单")
        if valid
        else ("unknown", "核心未调用场景算法，现有证据不足以证明重复目标无追加订单")
    )


def judge(action, result, config):
    """未知与条件不足不降格为通过；保留实际部分成交。"""
    memory = result.get("memory", {}).get("feitai", {})
    orders, trades = result.get("orders", []), result.get("trades", [])
    if memory.get("final_read_error") or "after" not in memory or "remaining_orders" not in memory:
        return "unknown", "缺少完整的最终持仓或挂单快照"
    if memory.get("remaining_orders") or any(not OrderStatus.is_completed(o["status"]) for o in orders):
        return "unknown", "存在未确认终态或残留挂单，需人工处理"
    if action == "invalid":
        return judge_invalid(memory, orders, config)
    if action == "reject":
        return judge_reject(orders, config)
    if action == "cancel":
        if not memory.get("pending_confirmed"):
            return "insufficient_conditions", "未确认合法挂起；可能已成交或回报缺失"
        if orders and all(o["status"] == OrderStatus.CANCELED for o in orders):
            if any(o.get("filled_volume", 0) for o in orders):
                return "failed", "已撤销剩余量，但发生部分成交；持仓与成交保留"
            return "passed", "实际撤单终态已确认且无残留"
        return "failed", "委托实际终态不是撤单成功"
    if memory.get("error"):
        return "insufficient_conditions", memory["error"].get("raw", "执行前置不足")
    target = (1 if config.side == "long" else -1) * config.lots if action == "open" else 0
    if memory.get("final_volume") != target:
        return "failed", "最终持仓未到约定目标（包含部分成交）"
    if action == "open" and not memory.get("requests"):
        return "passed", "重复目标保持到位，本次没有追加订单"
    if (
        orders
        and all(o["status"] == OrderStatus.FILLED for o in orders)
        and matched_fills(orders, trades, config.symbol)
    ):
        return "passed", "成交明细、订单终态和最终持仓一致"
    return "unknown", "缺少与订单数量一致的成交明细，不能只按最终仓位判定"


def matched_fills(orders, trades, symbol):
    """逐订单关联成交，聚合数量相等不能掩盖串单或缺失。"""
    quantities = {order["order_id"]: 0.0 for order in orders}
    identities = set()
    for trade in trades:
        order_id = trade.get("order_id")
        identity = (order_id, trade.get("trade_id"))
        if trade.get("symbol") != symbol or order_id not in quantities or identity in identities:
            return False
        identities.add(identity)
        quantities[order_id] += trade.get("trade_volume", 0)
    return all(math.isclose(quantities[o["order_id"]], o["volume"]) for o in orders)


def judge_invalid(memory, orders, config):
    error = memory.get("request_error", {})
    expected = "不符合 tick 或涨跌停" if config.invalid_case == "price" else "超过单笔上限"
    if expected in error.get("raw", "") and error.get("return_code") is None and not orders:
        return "passed", "命中本地目标校验；原生未发送需结合边界回归核对"
    return "insufficient_conditions", "未命中指定本地校验，不能以其他保护阻断替代"


def judge_reject(orders, config):
    rejected = [o for o in orders if o["status"] == OrderStatus.REJECTED]
    if not rejected:
        return "insufficient_conditions", "未取得目标柜台拒绝回报；本地拦截及同步拒绝另行留证"
    if config.expected_error_id is None:
        return "unknown", "取得拒绝但未配置飞泰确认的 expected_error_id，不能归因到指定条件"
    if any(config.expected_error_id in error_ids(o) for o in rejected):
        return "passed", "取得匹配的柜台拒绝码，原始条件与文字见证据"
    return "failed", "柜台拒绝码与预期不符"


def error_ids(value):
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"ErrorID", "error_id"}:
                found.append(child)
            else:
                found.extend(error_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(error_ids(child))
    return found


def day_counts(frame):
    """只选 account/per_day，撤单仅选择原生 cancel_order_ctp。"""
    if frame.get("stale") or frame.get("incomplete") or frame.get("pending_policy"):
        return None
    counts = {}
    for rule in frame.get("metrics", []) + frame.get("rules", []):
        if rule.get("scope") != "account" or rule.get("rule") != "per_day":
            continue
        operation = rule["operation"]
        if operation in {"place_order", "cancel_order_ctp"} and rule.get("value") is not None:
            if operation in counts and counts[operation] != rule["value"]:
                return None
            counts[operation] = rule["value"]
    return counts if len(counts) == 2 else None


def reconcile(before, after, events):
    """本地放行计数与逐笔事件核对，窗口／实例变化不计算伪增量。"""
    start, end = day_counts(before), day_counts(after)
    same = all(before.get(k) == after.get(k) for k in ("control_date", "timezone", "instance_id"))
    result = {
        "source": "Axile 本地风控放行计数",
        "cancel_operation": "cancel_order_ctp",
        "start": start,
        "end": end,
        "control_date": before.get("control_date"),
        "timezone": before.get("timezone"),
        "complete": False,
        "rows": [],
    }
    if start is None or end is None or not same:
        result["reason"] = "快照不完整、计数缺失、跨日或实例变更；增量未知"
        return result
    delta = {key: end[key] - start[key] for key in start}
    if min(delta.values()) < 0:
        result["reason"] = "计数回退；不推测重启恢复正确"
        return result
    seen, counted = set(), dict.fromkeys(start, 0)
    for event in events.get("data", []):
        uid = event.get("event_uid", event.get("id"))
        if uid is None or uid in seen:
            continue
        seen.add(uid)
        if event.get("operation") in counted:
            result["rows"].append(event)
            if event.get("counted"):
                counted[event["operation"]] += 1
    result.update(
        delta=delta,
        total=sum(delta.values()),
        counted_events=counted,
        complete=events.get("complete", False) and delta == counted,
    )
    if not result["complete"]:
        result["reason"] = "计数增量与本批次事件不一致或审计不完整，可能存在其他操作"
    return result
