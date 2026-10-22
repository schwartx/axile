"""迁移后的 API 边界回归：旧记录摘要与独立附件保留既有展示语义。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from axile.server.api.deps import HistoryPagination
from axile.server.api.routes.account_execution import execution_artifacts
from axile.server.api.routes.account_schedule import account_activity
from axile.server.business_facts.repository import read_execution_result, store_execution_facts
from axile.server.db.models import ExecuteRecord
from tests.unit.server._execution_test_support import build_account

_LEGACY_RAW = {
    "outcome": "error",
    "outcome_reason": "RuntimeError: 下单通道断开",
    "symbol_results": {"rb2610": {"outcome": "not_reached", "final_volume": 2}},
    "memory": {"message": "RuntimeError: 下单通道断开"},
}


class _Result:
    def __init__(self, rows, count):
        self._rows = rows
        self._count = count

    def scalar_one(self):
        return self._count

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """按调用顺序返回计数与行；覆盖列表/活动/附件三类读取路径。"""

    def __init__(self, plan):
        self._plan = list(plan)

    async def execute(self, _stmt):
        return self._plan.pop(0)

    async def scalar(self, _stmt):
        return self._plan.pop(0).scalar_one()

    async def get(self, _model, account_id):
        return SimpleNamespace(id=account_id)


@pytest.fixture
def pagination():
    return HistoryPagination(skip=0, limit=20)


def test_account_activity_normalizes_legacy():
    """真实活动页读取类型化展示列，完整证据仍保留迁移前的原字段。"""

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                session.add(build_account(id=1))
                record = ExecuteRecord(
                    id=7, execution_id="legacy-exec", account_id=1, is_success=0, created_at="2026-09-01T09:00:00"
                )
                session.add(record)
                await session.flush()
                await store_execution_facts(session, record, raw_input={}, raw_result=_LEGACY_RAW)
                await session.commit()
                assert await read_execution_result(session, record) == _LEGACY_RAW
                return json.loads((await account_activity(account_id=1, skip=0, limit=20)).body)
        finally:
            await engine.dispose()

    payload = asyncio.run(scenario())
    assert payload.count == 1
    activity = payload.data[0]
    assert activity.record.status == "FAILED"
    assert activity.record.error == "执行失败，具体原因见执行证据"


def test_execution_artifacts_normalize_summary_only(monkeypatch, pagination):
    """附件端点：仅执行摘要附件归一，其他附件形状不动。"""
    summary = SimpleNamespace(
        id=1,
        execution_id="legacy-exec",
        artifact_type="execution_summary",
        schema_version=2,
        content={"outcome": "blocked", "summary": {"symbols_total": 1}},
        created_at="2026-09-01T09:00:00",
        account_id=1,
    )
    target = SimpleNamespace(
        id=2,
        execution_id="legacy-exec",
        artifact_type="target_snapshot",
        schema_version=2,
        content={"target": {"rb2610": 1}},
        created_at="2026-09-01T09:00:00",
        account_id=1,
    )
    session = _FakeSession([_Result(None, 2), _Result([summary, target], None)])
    payload = asyncio.run(
        _call(execution_artifacts, session=session, execution_id="legacy-exec", pagination=pagination)
    )
    by_type = {item.artifact_type: item for item in payload.data}
    assert by_type["execution_summary"].content["status"] == "BLOCKED"
    assert by_type["execution_summary"].content["error"] == "执行受阻，具体原因见执行证据"
    assert by_type["execution_summary"].content["summary"] == {"symbols_total": 1}
    # 目标快照不是执行结果，不做任何推断。
    assert by_type["target_snapshot"].content == {"target": {"rb2610": 1}}


async def _call(fn, **kwargs):
    return await fn(**kwargs)
