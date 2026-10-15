"""只读流控快照的展示计算，与交易评估及持久化分离。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from axile.executor.account_control.models import AccountControlBucketType, AccountControlScopePolicy

if TYPE_CHECKING:
    from axile.executor.account_control.guard import AccountControlEvaluationContext, AccountControlGuard


def policy_fingerprint(policy: object) -> str:
    """返回稳定策略指纹，不包含账户凭据。"""
    return hashlib.sha256(json.dumps(policy, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _scope_rows(
    guard: AccountControlGuard,
    operation: str,
    scope: AccountControlScopePolicy,
    *,
    context: AccountControlEvaluationContext,
    symbol: str | None = None,
    group: bool = False,
) -> list[dict[str, Any]]:
    rows = []
    for key, bucket in (
        ("per_day", AccountControlBucketType.DAY),
        ("per_minute", AccountControlBucketType.MINUTE),
        ("min_interval_ms", None),
    ):
        rule = getattr(scope, key)
        if key == "min_interval_ms" and rule is None:
            continue
        limit = None if rule is None else rule.limit
        if bucket is None:
            assert limit is not None
            last = guard._get_last_allowed_at_ms(scope_key=operation, symbol=symbol, is_group=group)
            value = None if last is None else max(0, context.occurred_at_ms - last)
            remaining = None
            wait = 0 if value is None else max(0, limit - value)
        else:
            start = (
                context.window.day_bucket_start
                if bucket == AccountControlBucketType.DAY
                else context.window.minute_bucket_start
            )
            value = guard._get_count(
                control_date=context.window.control_date,
                bucket_type=bucket,
                bucket_start=start,
                scope_key=operation,
                symbol=symbol,
                is_group=group,
            )
            remaining = None if limit is None else max(0, limit - value)
            wait = 0
        rows.append(
            {
                "id": f"{'group' if group else symbol or 'account'}:{operation}:{key}",
                "operation": operation,
                "scope": "group" if group else "symbol" if symbol else "account",
                "symbol": symbol,
                "rule": key,
                "value": value,
                "limit": limit,
                "remaining": remaining,
                "estimated_wait_ms": wait,
                "on_trigger": None if rule is None else rule.on_trigger.value,
            }
        )
    return rows


def render_guard_snapshot(guard: AccountControlGuard) -> dict[str, Any]:
    """从冻结的 guard 输入生成规则、队列和本地放行计数。"""
    context = guard._current_context() if guard.policy is not None else None
    policy = None if guard.policy is None else guard.policy.model_dump(mode="json")
    rows: list[dict[str, Any]] = []
    if guard.policy is not None:
        assert context is not None
        operations = (
            set(guard.policy.operations)
            | {key[3] for key in guard._account_counters}
            | {key[2] for key in guard.baseline.account_counts}
            | {"place_order", "cancel_order"}
        )
        symbols = {key[1] for key in guard._symbol_counters} | {key[0] for key in guard.baseline.symbol_counts}
        if guard._quota_state is not None:
            operations |= {key[3] for key in guard._quota_state.account_counts}
            symbols |= {key[1] for key in guard._quota_state.symbol_counts}
        symbols |= {q.symbol for q in guard._wait_queue if q.symbol is not None}
        for operation in sorted(operations):
            op = guard.policy.operations.get(operation)
            rows.extend(
                _scope_rows(
                    guard, operation, AccountControlScopePolicy() if op is None else op.account, context=context
                )
            )
            if op is not None and op.symbol is not None:
                # 通配规则也展示，尚无合约实例时不伪造某个合约的用量。
                if not symbols:
                    templates = _scope_rows(guard, operation, op.symbol, context=context, symbol="每个合约")
                    for row in templates:
                        row.update(value=None, remaining=None)
                    rows.extend(templates)
                for symbol in sorted(symbols):
                    rows.extend(_scope_rows(guard, operation, op.symbol, context=context, symbol=symbol))
        for group, scope in guard.policy.groups.items():
            rows.extend(_scope_rows(guard, group, scope, context=context, group=True))
    now_ms = None if context is None else context.occurred_at_ms
    queue = [
        {
            "id": q.seq,
            "operation": q.operation,
            "symbol": q.symbol,
            "priority": q.priority,
            "entered_at_ms": q.entered_at_ms,
            "waited_ms": max(0, (now_ms or q.entered_at_ms) - q.entered_at_ms),
            **guard._monitor_waits.get(q.seq, {"kind": "queue", "hits": []}),
        }
        for q in sorted(guard._wait_queue, key=lambda q: (q.priority, q.seq))[:100]
    ]
    metrics = [
        {
            **row,
            "label": "今日报单"
            if row["operation"] == "place_order"
            else "今日撤单"
            if row["operation"] == "cancel_order"
            else row["operation"],
        }
        for row in rows
        if row["scope"] == "account" and row["rule"] == "per_day"
    ]
    return {
        "account_id": guard.account_id,
        "execution_id": guard.execution_id,
        "instance_id": guard.instance_id,
        "seq": guard._monitor_seq,
        "incomplete": guard.monitor_incomplete,
        "sampled_at_ms": now_ms,
        "status": "running" if guard._enabled() else "disabled",
        "timezone": None if guard.policy is None else guard.policy.timezone,
        "control_date": None if context is None else context.window.control_date,
        "minute_start": None if context is None else context.window.minute_bucket_start,
        "actual_policy": policy,
        "actual_fingerprint": policy_fingerprint(policy),
        "rules": rows,
        "metrics": metrics,
        "waiting": queue,
        "waiting_total": len(guard._wait_queue),
        "records": [event.model_dump(mode="json") for event in reversed(guard._events)],
        "quota_warnings": [warning.model_dump(mode="json") for warning in guard._quota_warnings.values()],
    }
