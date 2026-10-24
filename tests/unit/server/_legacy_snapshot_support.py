"""构造 0040 的冻结通知引用样本，供真实迁移读取验证。"""

import hashlib
import json

_EXECUTION_METADATA = {
    "id",
    "kind",
    "trigger_source",
    "notified_at",
    "execution_time",
    "outcome_reason",
    "channel_type",
    "status",
    "success",
    "outcome",
    "reason_code",
    "error",
    "is_test",
    "supplements",
}


def _wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def notification_reference(captured, event, event_id):
    def business(value):
        return {
            **{key: item for key, item in value.items() if key not in {"execution", "event_id", "event_type"}},
            "execution": {
                key: item for key, item in (value.get("execution") or {}).items() if key not in _EXECUTION_METADATA
            },
        }

    if _wire(business(captured)) != _wire(business(event)):
        return None
    execution = captured.get("execution")
    metadata = {key: item for key, item in (execution or {}).items() if key in _EXECUTION_METADATA}
    for key, item in metadata.items():
        if key == "supplements":
            if any(isinstance(field, (dict, list)) for step in item for field in step.values()):
                return None
        elif isinstance(item, (dict, list)):
            return None
    return {
        "$axile_notification_context": 1,
        "event_id": event_id,
        "event_sha256": hashlib.sha256(_wire(event).encode()).hexdigest(),
        "capture_sha256": hashlib.sha256(_wire(captured).encode()).hexdigest(),
        "root_order": list(captured),
        "root_metadata": {key: item for key, item in captured.items() if key in {"event_id", "event_type"}},
        "execution_state": "object" if isinstance(execution, dict) else "null" if "execution" in captured else "absent",
        "execution_order": list(execution or {}),
        "execution_metadata": metadata,
    }
