"""全局pytest配置和共享fixtures."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# 测试可能启动子进程；通过配置文件指针让父子进程共享同一个隔离数据库，
# 并确保任何服务端模块导入前都不会连接工作目录中的 ``axile.db``。
_TEST_RUNTIME = tempfile.TemporaryDirectory(prefix="axile-tests-")
_TEST_RUNTIME_PATH = Path(_TEST_RUNTIME.name)
_TEST_CONFIG_PATH = _TEST_RUNTIME_PATH / "config.toml"
_TEST_DATABASE_PATH = _TEST_RUNTIME_PATH / "axile.db"
_TEST_CONFIG_PATH.write_text(
    "\n".join(
        (
            f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{_TEST_DATABASE_PATH}"',
            f'app_log_dir = "{_TEST_RUNTIME_PATH / "logs"}"',
            'environment = "local"',
            "",
        )
    ),
    encoding="utf-8",
)
os.environ["AXILE_CONFIG_TOML"] = str(_TEST_CONFIG_PATH)


def require_native_store(*operations: str) -> ModuleType:
    """原生能力缺失时明确失败，避免旧二进制让持久化测试假通过。"""
    try:
        from axile.server._native import _core
    except ImportError as error:
        pytest.fail(f"需要当前源码构建的 Rust SQLite 扩展: {error}", pytrace=False)
    missing = [name for name in operations if not callable(getattr(_core, name, None))]
    if getattr(_core, "PUBLIC_HEAD", None) != "0034":
        missing.append("PUBLIC_HEAD=0034")
    if missing:
        pytest.fail(f"Rust SQLite 扩展缺少能力: {', '.join(missing)}；请先构建当前源码", pytrace=False)
    return _core


def native_store_call(path: Path | str, operation: str, request: object) -> object:
    """对隔离文件库调用真实原生业务阶段。"""
    core = require_native_store(operation)
    evidence_input = operation in getattr(core, "EVIDENCE_INPUT_STAGES", ())
    return getattr(core, operation)(str(path), json.dumps(request, ensure_ascii=False, allow_nan=evidence_input))


def initialize_native_database(path: Path | str, *, revision: str = "head") -> dict:
    """通过完整公开迁移链创建文件库，包含索引、触发器和版本记录。"""
    result = native_store_call(path, "migration_upgrade", {"public": revision})
    assert isinstance(result, dict), "migration_upgrade 必须返回迁移回执"
    return result


@pytest.fixture
def native_database(tmp_path: Path) -> Path:
    """每个用例使用真实 Rust 迁移初始化的独立 SQLite 文件。"""
    path = tmp_path / "native.db"
    initialize_native_database(path)
    return path


@pytest.fixture(scope="session")
def project_root() -> Path:
    """返回项目根目录."""
    return Path(__file__).parent.parent


@pytest.fixture(scope="session")
def tests_dir(project_root: Path) -> Path:
    """返回测试目录."""
    return project_root / "tests"


@pytest.fixture(scope="session")
def src_dir(project_root: Path) -> Path:
    """返回源码目录."""
    return project_root / "axile"


@pytest.fixture
def mock_logger():
    """创建模拟logger."""
    logger = MagicMock()
    logger.debug = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.exception = MagicMock()
    return logger


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """自动为测试添加标记."""
    for item in items:
        path = item.location[0]
        if "\\unit\\" in path or "/unit/" in path:
            item.add_marker(pytest.mark.unit)
        elif "\\contract\\" in path or "/contract/" in path:
            item.add_marker(pytest.mark.contract)
        elif "\\integration\\" in path or "/integration/" in path:
            item.add_marker(pytest.mark.integration)
        elif "\\live\\" in path or "/live/" in path:
            item.add_marker(pytest.mark.live)
            item.add_marker(pytest.mark.slow)
        if "ctp" in path.lower():
            item.add_marker(pytest.mark.ctp)
        if "server" in path.lower():
            item.add_marker(pytest.mark.server)
