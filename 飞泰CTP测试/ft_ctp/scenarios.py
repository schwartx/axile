"""额度边界、异常参数及可观测暂停时序。"""

import copy
import math
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psutil
from axile.executor.algorithms.utils.clock import clock_now_iso

from ft_ctp.api import ApiError
from ft_ctp.checks import day_counts
from ft_ctp.runner import Conditions


def quota_override(runner, *, waiting=False):
    """只收紧测试额度，保留原 preset 和全部其他保护。"""
    config = runner.config
    override = copy.deepcopy(runner.original.get("account_control_override") or {})
    operations = override.setdefault("operations", {})
    frame = runner.call("GET", runner.base + "/control/monitor")
    for rule in frame.get("rules", []):
        if rule.get("scope") == "account" and rule.get("operation") in {"place_order", "cancel_order"}:
            if rule.get("rule") == "per_day" and rule.get("limit") is not None and rule["limit"] < config.daily_limit:
                raise Conditions("测试日额度比现有保护宽松，拒绝放宽保护")
    for operation in ("place_order", "cancel_order"):
        entry = operations.setdefault(operation, {})
        entry["daily_warning_percent"] = config.warning_percent
        scope = entry.setdefault("account", {})
        scope["per_day"] = {"limit": config.daily_limit, "on_trigger": "block"}
    if waiting:
        operations["place_order"]["account"]["per_minute"] = {"limit": 0, "on_trigger": "wait"}
    runner.call("PATCH", runner.base, {"account_control_override": override})


def threshold(runner, step):
    config = runner.config
    if math.ceil(config.daily_limit * config.warning_percent / 100) != config.threshold:
        raise Conditions("threshold 必须等于 ceil(daily_limit * warning_percent / 100)")
    initial = runner.call("GET", runner.base + "/control/monitor")
    counts = day_counts(initial)
    if counts is None or counts["place_order"] > config.threshold - 1:
        raise Conditions("已有计数超过预警线前位置或未知，不重置计数制造证据")
    quota_override(runner)
    milestones, iterations = {}, []
    for number in range(config.max_requests // 2):
        frame = runner.call("GET", runner.base + "/control/monitor")
        current = day_counts(frame)
        if current is None:
            break
        value = current["place_order"]
        labels = {config.threshold - 1: "before", config.threshold: "at", config.threshold + 1: "after"}
        if value in labels:
            label = labels[value]
            milestones[label] = {
                "value": value,
                "frame": frame,
                "sampled_at": clock_now_iso(),
                "script_hint": {"source": "script", "triggered": value >= config.threshold},
            }
            runner.bundle.write(f"{step}/{label}.json", milestones[label])
        if value >= config.threshold + 1:
            break
        row = runner.trade("cancel", f"{step}/{number + 1:02d}")
        iterations.append(row)
        if row["status"] != "passed":
            break
    # 产品预警必须来自实际审计 metadata；脚本提示只记 source=script。
    warnings = []
    for row in iterations:
        path = runner.bundle.path / row["evidence"] / "control-events.json"
        if path.exists():
            import json

            data = json.loads(path.read_text())
            for event in data.get("data", []):
                metadata = event.get("metadata", {})
                if metadata.get("quota_warning") or metadata.get("quota_warnings"):
                    warnings.append(event)
    runner.bundle.write(
        f"{step}/product-warnings.json",
        {"source": "Axile control audit", "events": warnings},
    )
    runner.bundle.write(f"{step}/iterations.json", iterations)
    if set(milestones) != {"before", "at", "after"}:
        return {
            "scenario": step,
            "status": "insufficient_conditions",
            "reason": "保护阻断、计数不完整或请求预算不足；未覆盖全部位置",
        }
    return {
        "scenario": step,
        "status": "passed" if warnings else "unknown",
        "reason": "三个位置及产品预警事实已保留；通知送达另行核对"
        if warnings
        else "三个位置已保留，但缺少产品预警事实；脚本提示不能替代",
    }


def unknown_contract(runner, step):
    """通过独立精确查询探测未知合约，不运行用户组合函数。"""
    before = runner.call("GET", runner.base + "/control/monitor")
    error = None
    try:
        runner.call(
            "POST",
            "/instrument-catalog/query",
            {"account_id": runner.account_id, "symbols": [runner.config.unknown_symbol], "require_specs": True},
        )
    except ApiError as exc:
        error = str(exc)
    connection = runner.call("GET", runner.base + "/connection")
    runner.bundle.write(f"{step}/connection-after-probe.json", connection)
    if connection.get("status") != "ready":
        # 资料查询失败可能关闭共享会话；只恢复连接供只读残留核对，不重试执行或委托。
        runner.connect()
    after = runner.call("GET", runner.base + "/control/monitor")
    a, b = day_counts(before), day_counts(after)
    runner.bundle.write(
        f"{step}/unknown-contract.json",
        {
            "requested_symbol": runner.config.unknown_symbol,
            "stage": "instrument-catalog/query / CTP metadata",
            "error": error,
            "before": before,
            "after": after,
            "native_order_submission": "not invoked by script",
            "note": "此场景验证未知合约元数据校验，不将规划跳过当作直接 place_order 校验",
        },
    )
    matched = error and "CATALOG.NOT_FOUND" in error
    return {
        "scenario": step,
        "status": "passed" if matched and a is not None and a == b else "insufficient_conditions",
        "reason": "未知合约在 CTP 元数据边界被拒绝，报单计数不变" if matched else "未命中未知合约校验",
    }


def wait_point(runner, step, predicate):
    deadline = runner.clock.time() + runner.config.timeout_seconds
    samples = []
    while runner.clock.time() < deadline:
        sample = {
            "at": clock_now_iso(),
            "monitor": runner.call("GET", runner.base + "/control/monitor"),
            "account": runner.call("GET", runner.base),
        }
        sample["activity"] = runner.activity()
        current_id = sample["activity"].get("running_execution_id")
        if current_id and current_id not in runner.execution_ids:
            current_events = runner.api.pages(f"/account/executions/{current_id}/events")
            owned = any(
                e.get("details", {}).get("batch") == runner.bundle.index["batch"]
                for e in current_events.get("data", [])
            )
            if owned:
                runner.execution_ids.append(current_id)
                runner.pending_execution_ids.add(current_id)
                runner.active_execution = current_id
                runner.bundle.index["execution_ids"] = runner.execution_ids
        if runner.active_execution:
            sample["status"] = runner.call("GET", f"/account/executions/{runner.active_execution}")
            sample["events"] = runner.api.pages(f"/account/executions/{runner.active_execution}/events")
        samples.append(sample)
        runner.bundle.write(f"{step}/point-samples.json", samples)
        if predicate(sample):
            return sample
        runner.clock.sleep(runner.config.poll_seconds)
    raise Conditions("未观察到目标时序锚点，未发送暂停／终止操作")


def arm_pause(runner, step):
    config = runner.config
    point = config.pause_point
    holds = {"hold_seconds": 0, "before_send_hold_seconds": 0, "after_send_hold_seconds": 0}
    gate = {
        "ready": "hold_seconds",
        "before_send": "before_send_hold_seconds",
        "after_send": "after_send_hold_seconds",
    }[config.pause_checkpoint]
    # 等待额度必须立即进入风控；预先睡满 timeout 会耗尽锚点观察窗口。
    holds[gate] = 0 if point == "quota" else min(config.timeout_seconds, 3600)
    runner.configure("hold", step, **holds)
    if point == "idle":
        return
    if point in {"scheduled", "supplement"}:
        if config.observe_seconds < 65:
            raise Conditions("定时与补发场景 observe_seconds 至少 65，且应覆盖所选触发周期")
        patch = {"is_started": True, "cron_expr": config.cron_expr}
        if point == "supplement":
            patch["supplement"] = config.supplement or {"count": 1, "interval_minutes": 1}
            if config.observe_seconds < patch["supplement"]["interval_minutes"] * 60 + 5:
                raise Conditions("观察期必须覆盖一个补发周期并留出至少 5 秒回报时间")
            # 只安排下一分钟的一次基础触发，避免把下一轮新补发组误当旧组取消失败。
            next_minute = (math.floor(runner.clock.time() / 60) + 1) * 60
            moment = datetime.fromtimestamp(next_minute, ZoneInfo("Asia/Shanghai"))
            patch["cron_expr"] = f"{moment.minute} {moment.hour} {moment.day} {moment.month} *"
        runner.call("PATCH", runner.base, patch)
        schedule = runner.call("GET", runner.base + "/next_run_time")
        runner.bundle.write(f"{step}/armed-schedule.json", schedule)
        if not schedule.get("next_execution_times"):
            raise Conditions("没有可验证的下次实际调度时刻（可能非交易日），停止调度")
        if point == "scheduled":
            next_time = datetime.fromisoformat(schedule["next_execution_times"][0])
            if next_time.tzinfo is None:
                next_time = next_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            if next_time.timestamp() - runner.clock.time() + 2 > config.observe_seconds:
                raise Conditions("观察期未覆盖下次实际触发时刻，请延长 observe_seconds")
            return
        # 补发组必须由一次实际定时执行产生，不能伪造数据库。
        wait_point(
            runner,
            step,
            lambda s: s["account"].get("has_pending_supplement", False) and runner.active_execution is not None,
        )
        schedule = runner.call("GET", runner.base + "/next_run_time")
        runner.bundle.write(f"{step}/pending-supplement-schedule.json", schedule)
        return
    if point == "quota":
        quota_override(runner, waiting=True)
    first = runner.trigger(step)
    if point == "queued":
        initial = runner.call("GET", f"/account/executions/{first}")
        runner.bundle.write(f"{step}/initial-queue-status.json", initial)
        if initial.get("status") == "QUEUED":
            return
        second = runner.trigger(step + "/queued")
        if second == first:
            raise Conditions("请求被合并，尚未形成独立排队任务")
        wait_point(runner, step, lambda s: s["status"]["status"] == "QUEUED")
    elif point == "quota":
        wait_point(runner, step, lambda s: waiting_for_test_quota(s["monitor"]))
    else:
        wait_point(
            runner,
            step,
            lambda s: any(
                e.get("reason_code") == f"FT.CHECKPOINT.{config.pause_checkpoint}"
                for e in s.get("events", {}).get("data", [])
            ),
        )


def waiting_for_test_quota(monitor):
    """只认本场景的报单零额度，不把准备期间的查询节流误当作目标锚点。"""
    return any(
        wait.get("kind") == "quota"
        and any(
            hit.get("operation") == "place_order"
            and hit.get("rule_kind") == "per_minute"
            and hit.get("scope_type") == "account"
            and hit.get("limit") == 0
            for hit in wait.get("hits", [])
        )
        for wait in monitor.get("waiting", [])
    )


def observe_pause(runner, step, before):
    config = runner.config
    samples = []
    deadline = runner.clock.time() + config.observe_seconds
    initial_process = local_server_process(runner) if config.pause_point == "restart" else None
    if config.pause_point == "restart":
        runner.bundle.write(
            f"{step}/restart-window.json",
            {
                "opened_at": clock_now_iso(),
                "seconds": config.observe_seconds,
                "instruction": "在另一终端按现有运维方式重启服务",
                "local_server_process": initial_process,
            },
        )
    while runner.clock.time() < deadline:
        try:
            sample = pause_sample(runner)
        except ApiError as exc:
            if config.pause_point != "restart":
                raise
            sample = {"at": clock_now_iso(), "read_error": str(exc)}
        samples.append(sample)
        runner.bundle.write(f"{step}/observations.json", samples)
        runner.clock.sleep(min(config.poll_seconds, max(0, deadline - runner.clock.time())))
    complete_samples = [sample for sample in samples if "read_error" not in sample]
    if not complete_samples or "read_error" in samples[-1]:
        raise Conditions("缺少后续观察")
    final = complete_samples[-1]
    persisted = not final["account"].get("is_started") if config.pause_action == "pause_auto" else True
    if config.pause_point == "restart":
        final_process = local_server_process(runner)
        runner.bundle.write(f"{step}/restart-process.json", {"before": initial_process, "after": final_process})
        process_changed = initial_process and final_process and initial_process != final_process
        instance_changed = (
            before.get("instance_id")
            and final["monitor"].get("instance_id")
            and before["instance_id"] != final["monitor"]["instance_id"]
        )
        if not (process_changed or instance_changed):
            return {
                "scenario": step,
                "status": "insufficient_conditions",
                "reason": "观察期未发生可验证的服务实例切换，请按说明重启后再验",
            }
    if not final["orders"].get("complete") or final["orders"].get("data"):
        return {"scenario": step, "status": "unknown", "reason": "最终挂单不完整或有残留"}
    if not persisted:
        return {"scenario": step, "status": "failed", "reason": "暂停自动执行配置未持久化"}
    if config.pause_point == "supplement" and final["account"].get("has_pending_supplement"):
        return {"scenario": step, "status": "failed", "reason": "取消后仍存在剩余补发组"}
    if config.pause_point in {"scheduled", "supplement"} and any(
        s["activity"].get("running_execution_id") or s["activity"].get("pending_execution_id") for s in complete_samples
    ):
        return {"scenario": step, "status": "unknown", "reason": "观察期出现活跃执行，无法证明定时／补发已停止"}
    if config.pause_action != "pause_auto" or config.pause_point == "scheduled":
        a, b = day_counts(before), day_counts(final["monitor"])
        if a is None or b is None:
            return {"scenario": step, "status": "unknown", "reason": "终止后计数不完整，未证明无新请求"}
        if b["place_order"] > a["place_order"]:
            return {
                "scenario": step,
                "status": "failed",
                "reason": "请求操作后观察到新报单，需按实际结束时刻进一步核对",
            }
    return {
        "scenario": step,
        "status": "passed",
        "reason": "目标锚点、操作受理、实际结束与观察期残留已记录；恢复配置单独记录",
    }


def pause_sample(runner):
    return {
        "at": clock_now_iso(),
        "account": runner.call("GET", runner.base),
        "monitor": runner.call("GET", runner.base + "/control/monitor"),
        "orders": runner.call("GET", runner.base + "/orders?refresh=true"),
        "activity": runner.activity(),
    }


def local_server_process(runner):
    """idle monitor 可无实例 ID；仅对本地 API 记录监听进程身份作重启佐证。"""
    url = urlparse(getattr(runner.api, "url", ""))
    if url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return None
    port = url.port or (443 if url.scheme == "https" else 80)
    try:
        for connection in psutil.net_connections(kind="tcp"):
            if connection.status == psutil.CONN_LISTEN and connection.laddr.port == port and connection.pid:
                process = psutil.Process(connection.pid)
                return {"pid": process.pid, "created_at": process.create_time()}
    except (psutil.Error, OSError):
        return None
    return None


def pause(runner, step):
    config = runner.config
    try:
        arm_pause(runner, step)
        before = runner.call("GET", runner.base + "/control/monitor")
        runner.bundle.write(f"{step}/before-operation.json", before)
        if config.pause_action == "pause_auto":
            receipt = runner.call("PATCH", runner.base, {"is_started": False})
        else:
            try:
                receipt = runner.call(
                    "POST", runner.base + "/terminate", {"mode": config.terminate_mode, "reason": "FT 暂停时序验证"}
                )
            except ApiError as exc:
                if config.pause_point != "idle" or exc.status != 409:
                    raise
                receipt = {"accepted": False, "http_status": 409, "reason": "无活跃执行，符合当前产品语义"}
        runner.bundle.write(f"{step}/operation-receipt.json", receipt)
        for execution_id in list(dict.fromkeys(runner.execution_ids)):
            runner.wait(
                execution_id,
                step + "/" + execution_id,
                extra_seconds=config.timeout_seconds if config.pause_action == "pause_auto" else 0,
            )
            runner.collect(execution_id, step + "/" + execution_id)
        ended = runner.call("GET", runner.base + "/control/monitor")
        runner.bundle.write(f"{step}/after-actual-end.json", ended)
        return observe_pause(runner, step, ended)
    finally:
        if config.pause_point in {"scheduled", "supplement"}:
            runner.call("PATCH", runner.base, {"is_started": False, "supplement": None})


def export_account(runner, step="account-evidence"):
    """账户范围导出补足独立账户操作和定时触发的 execution。"""
    date = datetime.fromtimestamp(runner.clock.time(), ZoneInfo("Asia/Shanghai")).date().isoformat()
    data = runner.call(
        "GET",
        runner.base + f"/evidence?start_date={date}&end_date={date}&profile=shareable&include_logs=true",
        binary=True,
    )
    runner.save_zip(f"{step}/product-evidence.zip", data)
    return {"scenario": step, "status": "passed", "reason": "账户日期范围产品证据已导出；场景缺失材料仍见索引"}


def dispatch(runner, action, step):
    if action == "stats":
        return runner.stats(step)
    if action == "threshold":
        return threshold(runner, step)
    if action == "pause":
        return pause(runner, step)
    if action == "evidence":
        return export_account(runner, step)
    if action == "invalid" and runner.config.invalid_case == "unknown":
        return unknown_contract(runner, step)
    if action == "reject" and not runner.config.broker_condition:
        raise Conditions("尚未填写飞泰认可的 broker_condition；柜台拒绝场景待联机条件确认")
    return runner.trade(action, step)
