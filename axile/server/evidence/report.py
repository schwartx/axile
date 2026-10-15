"""从冻结事实派生报告；业务结论沿用已有对账，不从任务成功推断成交。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from typing import Any

from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_account_assets import is_degraded_snapshot_source
from axile.server.evidence.models import EvidenceSnapshot, ExecutionEvidence
from axile.server.execution.legacy_compat import normalize_legacy_result

_TASK_LABELS = {
    "QUEUED": "排队中",
    "RUNNING": "运行中",
    "TERMINATING": "终止中",
    "TERMINATED": "已终止",
    "SUCCEEDED": "已结束",
    "FAILED": "执行失败",
    "UNKNOWN": "任务状态未知",
}
_OUTCOME_LABELS = {
    "completed": "执行器报告完成",
    "not_reached": "未到位",
    "error": "执行失败",
    "blocked": "执行受阻",
    "terminated": "已终止",
    "unknown": "业务结果未知",
}


def as_dict(value: Any) -> dict[str, Any]:
    """非字典证据不参与推断。"""
    return value if isinstance(value, dict) else {}


def folder(key: str) -> str:
    """历史业务标识不作为任意归档路径，防止路径穿越。"""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,96}", key):
        return key
    return "execution-" + hashlib.sha256(key.encode()).hexdigest()[:24]


def csv_bytes(rows: list[dict[str, Any]], fields: list[str]) -> bytes:
    """写出 UTF-8 CSV；空值保持空白，字符串公式转义，不改动数值负号。"""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = {}
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            safe[key] = value
        writer.writerow(safe)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def result_payload(item: ExecutionEvidence) -> tuple[dict[str, Any], str]:
    """优先取结果账；结果缺失时保留摘要附件，并标注来源。"""
    if item.record is not None:
        raw = item.record.get("raw_result")
        if isinstance(raw, dict) and raw:
            return as_dict(normalize_legacy_result(raw)), "record.json#/raw_result"
    for artifact in item.artifacts:
        if artifact["artifact_type"] == "execution_summary":
            return as_dict(normalize_legacy_result(artifact["content"])), f"artifacts.json#id={artifact['id']}"
    return {}, "unavailable"


def _artifact(item: ExecutionEvidence, kind: str) -> dict[str, Any]:
    return next((as_dict(a.get("content")) for a in item.artifacts if a["artifact_type"] == kind), {})


def reconciliation(item: ExecutionEvidence, result: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """只消费已经持久化的权威对账；旧记录缺失时不补造前后持仓。"""
    artifact = _artifact(item, "execution_summary")
    if isinstance(artifact.get("reconciliation"), dict):
        return artifact["reconciliation"], "artifacts.json#execution_summary/reconciliation"
    if isinstance(result.get("reconciliation"), dict):
        return result["reconciliation"], "record.json#/raw_result/reconciliation"
    return {}, "unavailable"


def order_trade_rows(item: ExecutionEvidence, result: dict[str, Any], source: str) -> tuple[list[dict], list[dict]]:
    """原生订单与成交分别导出，保持未知状态和稳定原生关联。"""
    orders: list[dict] = []
    trades: list[dict] = []
    symbols = as_dict(result.get("symbol_results"))
    groups = [(symbol, as_dict(value)) for symbol, value in symbols.items()]
    if isinstance(result.get("orders"), list) or isinstance(result.get("trades"), list):
        groups.append(("", result))
    seen_orders: set[str] = set()
    seen_trades: set[str] = set()
    for symbol, data in groups:
        _append_rows(orders, seen_orders, data.get("orders"), symbol, source, "order_id")
        _append_rows(trades, seen_trades, data.get("trades"), symbol, source, "trade_id")
    return orders, trades


def _append_rows(target: list[dict], seen: set[str], values: Any, symbol: str, source: str, identity: str) -> None:
    if not isinstance(values, list):
        return
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            continue
        # 仅去重有稳定身份的相同快照。缺失身份时，不能猜测两笔相同成交是重复回报。
        key = json.dumps([symbol, raw], ensure_ascii=False, sort_keys=True)
        if raw.get(identity) and key in seen:
            continue
        if raw.get(identity):
            seen.add(key)
        target.append(
            {
                **raw,
                "symbol": raw.get("symbol") or symbol,
                "source_path": source,
                "source_record_id": raw.get(identity) or index,
            }
        )


def _coverage(item: ExecutionEvidence, task_status: str) -> dict[str, Any]:
    types = {a["artifact_type"] for a in item.artifacts}
    pending = task_status in {"QUEUED", "RUNNING", "TERMINATING"}
    expected = {"environment_snapshot"}
    if task_status != "QUEUED":
        expected.add("account_snapshot_before")
        if (item.intent or {}).get("kind") == "rebalance":
            expected.update({"standard_input", "target_snapshot", "trade_rules_snapshot"})
    if not pending:
        expected.update({"execution_summary", "account_snapshot"})
    missing = sorted(expected - types)
    issues = []
    if item.intent is None:
        issues.append("intent_unavailable")
    if not pending and item.record is None:
        issues.append("result_record_unavailable")
    if not item.events and task_status != "QUEUED":
        issues.append("execution_events_unavailable")
    if any(s.get("status") != "closed" for s in item.sessions):
        issues.append("control_session_not_cleanly_finished")
    for kind in ("account_snapshot_before", "account_snapshot"):
        content = _artifact(item, kind)
        if not content:
            continue
        assets = as_dict(content.get("account_assets"))
        source = content.get("source") or assets.get("source")
        if is_degraded_snapshot_source(source) or not isinstance(assets.get("positions"), list):
            issues.append(f"{kind}_unavailable")
    if "counter_bucket_context_time_assumed" in item.selection_reasons:
        issues.append("control_counter_activity_time_unknown")
    return {
        "read_complete": True,
        "coverage": "provisional" if pending else "incomplete" if missing or issues else "available",
        "missing_artifacts": missing,
        "issues": issues,
        "audit_delivery": "best_effort; absence_of_missing_items_does_not_prove_all_callbacks_were_persisted",
    }


def _problems(item: ExecutionEvidence) -> list[dict[str, Any]]:
    """提取异常事实，区分同步返回码与异步柜台错误码。"""
    problems = []
    for event in item.events:
        if event.get("status") in {"WARNING", "ERROR"}:
            problems.append(
                {
                    "symbol": event.get("symbol"),
                    "reason_code": event.get("reason_code"),
                    "stage": event.get("reason_family"),
                    "source_path": f"events.jsonl#id={event['id']}",
                }
            )
    for event in item.controls:
        if event.get("decision") != "blocked" and event.get("outcome") not in {
            "broker_rejected",
            "broker_sync_rejected",
            "downstream_error",
            "policy_blocked",
        }:
            continue
        metadata = as_dict(event.get("metadata"))
        problems.append(
            {
                "symbol": event.get("symbol"),
                "reason_code": event.get("outcome"),
                "operation": event.get("operation"),
                "return_code": metadata.get("return_code"),
                "error_id": metadata.get("error_id"),
                "stage": metadata.get("error_stage") or event.get("decision"),
                "source_path": f"control-events.jsonl#id={event['id']}",
            }
        )
    return problems


def execution_report(item: ExecutionEvidence) -> tuple[dict[str, Any], list[dict], list[dict], list[dict]]:
    """将任务、业务结论、持仓核对和证据覆盖情况分开输出。"""
    result, source = result_payload(item)
    recon, recon_source = reconciliation(item, result)
    orders, trades = order_trade_rows(item, result, source)
    task_status = (item.intent or {}).get("status") or result.get("task_status") or "UNKNOWN"
    raw_rows = recon.get("symbols")
    rows = raw_rows if isinstance(raw_rows, list) else []
    known_pending = [o.get("order_id") for o in orders if o.get("status") in OrderStatus.ACTIVE_STATUSES]
    unknown_orders = [
        o.get("order_id")
        for o in orders
        if o.get("status") not in OrderStatus.ACTIVE_STATUSES + OrderStatus.COMPLETED_STATUSES
    ]
    environment = _artifact(item, "environment_snapshot") or None
    report = {
        "key": item.key,
        "execution_id": item.execution_id,
        "task_status": task_status,
        "output_status": result.get("status"),
        "business_outcome": result.get("outcome") or "unknown",
        "result_source": source,
        "reconciliation_source": recon_source,
        "selection_reasons": item.selection_reasons,
        "created_at": (item.intent or item.record or {}).get("created_at"),
        "finished_at": (item.intent or {}).get("finished_at"),
        "orders": len(orders),
        "trades": len(trades),
        "known_pending_order_ids": known_pending,
        "unknown_order_status_ids": unknown_orders,
        "order_scope": "orders_observed_in_this_execution; not_all_current_account_orders",
        "execution_environment": environment,
        "account_snapshot_sources": as_dict(recon.get("account")),
        "problems": _problems(item),
        "coverage": _coverage(item, task_status),
    }
    return report, orders, trades, [{**row, "source_path": recon_source} for row in rows if isinstance(row, dict)]


def markdown_cell(value: Any) -> str:
    """转义证据文本，避免表格断行及 HTML 注入。"""
    if value is None:
        return "未知"
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace(chr(96), "\\" + chr(96))
        .replace("\n", " ")
        .replace("\r", " ")
    )


def execution_markdown(report: dict[str, Any], rows: list[dict]) -> str:
    """生成无需在线服务即可阅读的逐次执行摘要。"""
    state = _TASK_LABELS.get(report["task_status"], report["task_status"])
    outcome = _OUTCOME_LABELS.get(report["business_outcome"], report["business_outcome"])
    coverage = report["coverage"]
    lines = [
        f"# 执行证据 {markdown_cell(report['key'])}",
        "",
        f"任务：{markdown_cell(state)}；业务结论：{markdown_cell(outcome)}。",
        f"已记录订单 {report['orders']} 笔，成交记录 {report['trades']} 笔。",
        f"已知仍活跃订单：{markdown_cell(report['known_pending_order_ids'])}；状态未知订单：{markdown_cell(report['unknown_order_status_ids'])}。",
        "以上订单范围限本次执行，不代表导出时账户全部挂单。",
        f"账户持仓来源：{markdown_cell(report['account_snapshot_sources'])}；账户快照退化时，对账中的单合约持仓可能来自执行内较早确认的回报。",
        "",
        "| 合约 | 执行前 | 目标 | 执行后 | 净成交 | 到位 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    lines.extend(
        "| "
        + " | ".join(markdown_cell(row.get(k)) for k in ("symbol", "before", "target", "after", "filled", "reached"))
        + " |"
        for row in rows
    )
    if not rows:
        lines.append("\n没有可用的逐合约持仓对账，不能从任务成功推断成交或到位。")
    if report["problems"]:
        lines.extend(["", "| 合约 | 原因码 | 同步返回码 | 异步错误码 | 阶段 | 依据 |", "|---|---|---|---|---|---|"])
        lines.extend(
            "| "
            + " | ".join(
                markdown_cell(p.get(k))
                for k in ("symbol", "reason_code", "return_code", "error_id", "stage", "source_path")
            )
            + " |"
            for p in report["problems"]
        )
    lines.extend(
        [
            "",
            f"材料状态：{coverage['coverage']}；本次数据库读取已完成。",
            f"缺失附件：{markdown_cell(coverage['missing_artifacts'])}。",
            f"其他缺口：{markdown_cell(coverage['issues'])}。",
            "审计写入为尽力而为，材料齐备也不证明所有柜台回报均已留存。",
            f"执行当时环境：{'见 environment_snapshot 附件' if report['execution_environment'] else '未知，历史执行未保存环境快照'}。",
            "",
            "依据：record.json、events.jsonl、artifacts.json、control-events.jsonl；",
            f"结果来源：{report['result_source']}；对账来源：{report['reconciliation_source']}。",
        ]
    )
    return "\n".join(lines) + "\n"


def control_summary(snapshot: EvidenceSnapshot) -> list[dict[str, Any]]:
    """按事件事实汇总，外层撤单与原生撤单分别列示，绝不相加。"""
    counts: Counter = Counter()
    for item in snapshot.executions.values():
        for row in item.controls:
            if snapshot.window is not None and not snapshot.window.contains_ms(row["occurred_at_ms"]):
                continue
            counts[(row["control_date"], row["operation"], row["decision"], row["outcome"], row["counted"])] += 1
    return [
        {
            "control_date": key[0],
            "operation": key[1],
            "decision": key[2],
            "outcome": key[3],
            "counted": key[4],
            "events": value,
        }
        for key, value in sorted(counts.items())
    ]


def index_markdown(snapshot: EvidenceSnapshot, reports: list[dict[str, Any]], account_name: str) -> str:
    """总摘要将每日统计范围和整次执行上下文的区别写清。"""
    scope = snapshot.window.describe() if snapshot.window else {"basis": "explicit_execution"}
    lines = [
        "# Axile 执行证据包",
        "",
        f"账户：{markdown_cell(account_name)}。",
        f"数据库快照：{snapshot.snapshot_at}；选择范围：{markdown_cell(scope)}。",
        "各执行目录保留整次执行上下文；日期统计只计算窗口内的控制事件。",
        "",
        "| 执行 | 任务 | 业务结论 | 订单 | 成交 | 材料 |",
        "|---|---|---|---:|---:|---|",
    ]
    for report in reports:
        label = markdown_cell(report["key"])
        path = f"executions/{folder(report['key'])}/summary.md"
        lines.append(
            f"| [{label}]({path}) | {markdown_cell(report['task_status'])} | {markdown_cell(report['business_outcome'])} | {report['orders']} | {report['trades']} | {report['coverage']['coverage']} |"
        )
    lines.extend(
        [
            "",
            "控制统计：control-summary.csv 按 control_date、操作、决策、结果和 counted 分组。",
            "这是本地控制事件口径；放行不等于柜台接受，报单不等于成交。",
            "cancel_order 和 cancel_order_ctp 独立列示；日/分钟/合约计数增量不重复累计。",
            "counter-deltas.jsonl 保留原始窗口和作用域，供逐项对照。",
            "分享版保留真实交易数量、价格、时间和原生订单/成交编号；身份字段使用别名。",
            "manifest.json 包含文件校验值、来源计数、缺失说明和脱敏规则。",
        ]
    )
    return "\n".join(lines) + "\n"
