"""读时归一的 API 边界回归：旧记录经记录列表 / 活动流 / 附件端点必须带契约字段。"""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from axile.server.api.deps import HistoryPagination
from axile.server.api.routes.account_execution import execution_artifacts
from axile.server.api.routes.account_schedule import account_activity

_LEGACY_RAW = {
    "outcome": "error",
    "outcome_reason": "RuntimeError: 下单通道断开",
    "symbol_results": {"rb2610": {"outcome": "not_reached", "final_volume": 2}},
    "memory": {"message": "RuntimeError: 下单通道断开"},
}


def _legacy_record() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        execution_id="legacy-exec",
        account_id=1,
        raw_input={},
        raw_result=dict(_LEGACY_RAW),
        is_success=0,
        created_at="2026-09-01T09:00:00",
    )


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

    def begin_nested(self):
        return nullcontext()

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
    """活动流端点：内嵌执行记录同样读时归一。"""
    session = _FakeSession(
        [
            _Result(None, 1),  # execution count
            _Result(None, 0),  # skip count
            _Result([SimpleNamespace(id=7, kind=0)], None),  # page headers
            _Result([_legacy_record()], None),  # execution rows
            _Result([], None),  # skip rows
        ]
    )
    payload = asyncio.run(_call(account_activity, session=session, account_id=1, skip=0, limit=20))
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
