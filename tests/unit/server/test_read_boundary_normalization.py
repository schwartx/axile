"""迁移后的 API 边界回归：旧记录摘要与独立附件保留既有展示语义。"""

import asyncio
import json

from axile.server.api.routes.account_schedule import account_activity
from axile.server.business_facts.repository import read_execution_result
from axile.server.db.models import ExecuteRecord
from axile.server.native_store import native_operation
from tests.unit.server._runtime_db_support import runtime_database

_LEGACY_RAW = {
    "outcome": "error",
    "outcome_reason": "RuntimeError: 下单通道断开",
    "symbol_results": {"rb2610": {"outcome": "not_reached", "final_volume": 2}},
    "memory": {"message": "RuntimeError: 下单通道断开"},
}


def test_account_activity_normalizes_legacy(tmp_path, monkeypatch):
    """活动页读取冻结展示列；完整证据保留原字段。"""
    from axile.server._native import _core
    from axile.server.api.routes import account_schedule

    path = tmp_path / "activity.db"
    monkeypatch.setattr(account_schedule, "analysis_store", lambda: _core.Store(str(path)))

    async def scenario():
        async with runtime_database(path):
            stored = await native_operation(
                "persist_execute_record",
                {
                    "account_id": 1,
                    "execution_id": "legacy-exec",
                    "is_success": 0,
                    "created_at": "2026-09-01T09:00:00",
                    "raw_input": {},
                    "raw_result": _LEGACY_RAW,
                    "input_id": "legacy-input",
                    "result_input_id": "legacy-result-input",
                },
            )
            record = ExecuteRecord.model_validate(stored)
            # 旧存储已补任务终态和数量存在性；业务原字段仍须完整保留。
            assert await read_execution_result(record) == {
                **_LEGACY_RAW,
                "task_status": "FAILED",
                "symbol_results": {
                    "rb2610": {
                        **_LEGACY_RAW["symbol_results"]["rb2610"],
                        "availability": {"target_volume": "absent", "final_volume": "value"},
                    }
                },
            }
            return json.loads((await account_activity(account_id=1, skip=0, limit=20)).body)

    payload = asyncio.run(scenario())
    assert payload["count"] == 1
    activity = payload["data"][0]
    assert activity["record"]["status"] == "FAILED"
    assert activity["record"]["error"] == "执行失败，具体原因见执行证据"
