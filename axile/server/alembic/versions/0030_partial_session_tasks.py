"""修正证据明确的部分休市任务终态，保留未完成结果。"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

SESSION_CLOSED = "COMMON.SESSION.CLOSED"


def _partial_session(raw):
    """固定此版本的历史证据规则，禁止将真实失败或终止改成休市。"""
    if not isinstance(raw, dict):
        return False
    if raw.get("status") != "PARTIAL" or raw.get("reason_code") != SESSION_CLOSED:
        return False
    if raw.get("task_status") not in (None, "FAILED", "PARTIAL") or raw.get("termination"):
        return False
    if raw.get("success") is True:
        return False
    symbols = raw.get("symbol_results")
    if not isinstance(symbols, dict) or not symbols:
        return False
    kinds = {_symbol_kind(value) for value in symbols.values()}
    return kinds == {"closed", "open"}


def _symbol_kind(value):
    if not isinstance(value, dict):
        return "error"
    if value.get("status") in ("SUCCEEDED", "NOOP"):
        return "open"
    if (
        value.get("status") == "BLOCKED"
        and value.get("reason_code") == SESSION_CLOSED
        and not value.get("orders")
        and not value.get("trades")
    ):
        return "closed"
    return "error"


def _load_result(value):
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def upgrade():
    """仅修正没有异常或终止证据的历史误分类。"""
    connection = op.get_bind()
    rows = (
        connection.execute(
            sa.text(
                "SELECT i.execution_id, r.id, r.raw_result FROM execution_intent i "
                "JOIN executerecord r ON r.execution_id=i.execution_id AND r.account_id=i.account_id "
                "WHERE i.status='FAILED' AND r.is_success=0 "
                "AND (i.error IS NULL OR i.error='') AND i.cancel_requested_at IS NULL "
                "AND i.cancel_reason IS NULL AND i.terminate_mode IS NULL"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        raw = _load_result(row["raw_result"])
        if not _partial_session(raw):
            continue
        raw["task_status"] = "PARTIAL"
        if not raw.get("outcome_reason"):
            raw["outcome_reason"] = raw.get("error")
        connection.execute(
            sa.text("UPDATE execution_intent SET status='PARTIAL' WHERE execution_id=:execution_id"),
            {"execution_id": row["execution_id"]},
        )
        connection.execute(
            sa.text("UPDATE executerecord SET raw_result=:raw WHERE id=:id"),
            {"id": row["id"], "raw": json.dumps(raw, ensure_ascii=False)},
        )


def downgrade():
    """回退任务状态兼容旧代码，业务结果和休市证据保持不变。"""
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, raw_result FROM executerecord")).mappings().all()
    for row in rows:
        raw = _load_result(row["raw_result"])
        if not isinstance(raw, dict) or raw.get("task_status") != "PARTIAL":
            continue
        raw["task_status"] = "FAILED"
        connection.execute(
            sa.text("UPDATE executerecord SET raw_result=:raw WHERE id=:id"),
            {"id": row["id"], "raw": json.dumps(raw, ensure_ascii=False)},
        )
    connection.execute(sa.text("UPDATE execution_intent SET status='FAILED' WHERE status='PARTIAL'"))
