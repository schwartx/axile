"""独立 SQL oracle 的公开摘要字段；不依赖生产响应模型。"""


def execution_summary(value):
    """用明确字段和默认值组装对照值。"""
    data = value if isinstance(value, dict) else value.model_dump(mode="json")
    return {
        "execution_id": data.get("execution_id", None),
        "is_success": data.get("is_success", None),
        "input_id": data.get("input_id", None),
        "result_input_id": data.get("result_input_id", None),
        "status": data.get("status", None),
        "task_status": data.get("task_status", None),
        "error": data.get("error", None),
        "outcome": data.get("outcome", None),
        "outcome_reason": data.get("outcome_reason", None),
        "reason_code": data.get("reason_code", None),
        "msg": data.get("msg", None),
        "execution_kind": data.get("execution_kind", None),
        "channel_type": data.get("channel_type", None),
        "execution_time": data.get("execution_time", None),
        "success": data.get("success", None),
        "display_status": data.get("display_status", None),
        "display_error": data.get("display_error", None),
        "display_outcome": data.get("display_outcome", None),
        "display_outcome_reason": data.get("display_outcome_reason", None),
        "termination_reason": data.get("termination_reason", None),
        "termination_mode": data.get("termination_mode", None),
        "termination_trigger": data.get("termination_trigger", None),
        "termination_requested_at": data.get("termination_requested_at", None),
        "termination_acked_at": data.get("termination_acked_at", None),
        "termination_finished_at": data.get("termination_finished_at", None),
        "termination_cancel_attempted": data.get("termination_cancel_attempted", None),
        "termination_forced": data.get("termination_forced", None),
        "termination_cancel_unconfirmed": data.get("termination_cancel_unconfirmed", None),
        "id": data.get("id", None),
        "account_id": data.get("account_id", None),
        "created_at": data.get("created_at", None),
        "symbol_results": data.get("symbol_results", None),
        "trade_count": data.get("trade_count", None),
        "symbol_count": data.get("symbol_count", None),
    }
