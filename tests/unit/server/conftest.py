"""服务端单元测试共享夹具."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.conftest import _TEST_DATABASE_PATH, initialize_native_database


@pytest.fixture(scope="session", autouse=True)
def _ensure_database_schema() -> Iterator[None]:
    """启动前迁移隔离文件库，建表行为与实际发布版本一致。"""
    initialize_native_database(_TEST_DATABASE_PATH)
    yield


@pytest.fixture(autouse=True)
def _isolate_connection_runtime():
    from axile.server.connection_runtime import clear_sessions

    clear_sessions()
    yield
    clear_sessions()
