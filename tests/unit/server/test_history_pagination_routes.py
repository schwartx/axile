"""历史查询路由的统一分页边界测试。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.deps import get_db, get_scheduler
from axile.server.api.routes import account as account_routes


class _UnusedSession:
    """非法参数到达数据库层即视为测试失败。"""

    async def get(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid pagination must not query the database")

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid pagination must not query the database")


class _Result:
    def __init__(self, count: int = 0) -> None:
        self._count = count

    def scalar_one(self) -> int:
        return self._count

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[object]:
        return []


class _PagingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def get(self, *_args: object, **_kwargs: object) -> object:
        return type("Account", (), {"id": 1})()

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result()


class _Scheduler:
    def get_job(self, _job_id: str) -> None:
        return None


def _build_app(session: object | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(account_routes.router)

    async def _override_get_db() -> AsyncGenerator[object, None]:
        yield _UnusedSession() if session is None else session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_scheduler] = lambda: _Scheduler()
    return app


@pytest.mark.parametrize(
    "path",
    [
        "/account/execute_records/1",
        "/account/portfolio_records/1",
        "/account/executions/execution-1/events",
        "/account/executions/execution-1/artifacts",
    ],
)
@pytest.mark.parametrize("params", [{"skip": -1}, {"limit": 0}, {"limit": 501}])
def test_history_routes_reject_out_of_range_pagination_before_database_access(
    path: str, params: dict[str, int]
) -> None:
    """四个历史端点必须在执行查询前拒绝越界分页参数。"""
    response = TestClient(_build_app()).get(path, params=params)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/account/execute_records/1",
        "/account/portfolio_records/1",
        "/account/executions/execution-1/events",
        "/account/executions/execution-1/artifacts",
    ],
)
def test_history_routes_accept_pagination_bounds_and_apply_limit(path: str) -> None:
    """允许最大页长，并把请求上限传递到每个端点的实际查询。"""
    limit = 500
    session = _PagingSession()
    response = TestClient(_build_app(session)).get(path, params={"skip": 0, "limit": limit})

    assert response.status_code == 200
    assert response.json() == {"data": [], "count": 0}
    assert len(session.statements) == 2
    assert limit in session.statements[-1].compile().params.values()
