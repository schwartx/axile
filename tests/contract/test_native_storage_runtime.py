"""原生存储的运行时边界与公开 API 验收。"""

from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


def _guard_first_party_imports() -> None:
    import builtins
    import importlib
    import sys

    blocked = {"sqlite3", "_sqlite3", "aiosqlite", "sqlalchemy", "sqlmodel", "alembic", "apsw", "pysqlite3"}
    original_import = builtins.__import__
    original_import_module = importlib.import_module

    def check(name: str, namespace: str) -> None:
        if namespace.partition(".")[0] == "axile" and name.partition(".")[0] in blocked:
            raise AssertionError(f"first-party Python storage import: {namespace} -> {name}")

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        namespace = globals if globals is not None else sys._getframe(1).f_globals
        check(name, namespace.get("__name__", ""))
        return original_import(name, globals, locals, fromlist, level)

    def guarded_import_module(name, package=None):
        check(name, sys._getframe(1).f_globals.get("__name__", ""))
        return original_import_module(name, package)

    builtins.__import__ = guarded_import
    importlib.import_module = guarded_import_module


def _run_isolated(tmp_path: Path, probe: Callable[..., None], *arguments: str) -> None:
    spec = importlib.util.find_spec("axile")
    assert spec is not None and spec.origin is not None
    package_root = Path(spec.origin).resolve().parent
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").is_file():
        assert package_root == source_root / "axile", "editable import points outside the tested worktree"
    database = tmp_path / "contract.db"
    config = tmp_path / "config.toml"
    config.write_text(
        f'sqlalchemy_database_uri = "sqlite+aiosqlite:///{database.as_posix()}"\n'
        f'app_log_dir = "{(tmp_path / "logs").as_posix()}"\n'
        'environment = "local"\n',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["AXILE_CONFIG_TOML"] = str(config)
    environment["AXILE_CONTRACT_DB"] = str(database)
    environment["AXILE_CONTRACT_PACKAGE_ROOT"] = str(package_root)
    if (package_root.parent / "pyproject.toml").is_file():
        environment["PYTHONPATH"] = str(package_root.parent)
    else:
        environment.pop("PYTHONPATH", None)
    script = "\n".join(
        (
            inspect.getsource(_guard_first_party_imports),
            "_guard_first_party_imports()",
            "import os\nfrom pathlib import Path\nimport axile",
            'assert Path(axile.__file__).resolve().parent == Path(os.environ["AXILE_CONTRACT_PACKAGE_ROOT"])',
            inspect.getsource(probe),
            f"{probe.__name__}(*{arguments!r})",
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _assert_import_guard() -> None:
    import importlib

    for statement in (
        "import sqlite3",
        "from sqlalchemy import create_engine",
        "__import__('aiosqlite')",
        "importlib.import_module('alembic')",
    ):
        try:
            exec(statement, {"__name__": "axile.contract_probe", "importlib": importlib})
        except AssertionError as error:
            assert "first-party Python storage import" in str(error)
        else:
            raise AssertionError(f"storage guard allowed {statement}")
    exec("import sqlite3", {"__name__": "third_party_sdk.probe"})


def test_storage_import_guard_allows_sdk_internals(tmp_path: Path) -> None:
    """只拦截公共核心自己的存储导入。"""
    _run_isolated(tmp_path, _assert_import_guard)


def _assert_native_migrations() -> None:
    import json
    import os
    import sqlite3
    from contextlib import closing
    from pathlib import Path

    from axile.server._native import _core
    from axile.server.core.db import initialize_database
    from axile.server.native_store import native_operation_sync

    assert _core.PUBLIC_HEAD == "0039"
    assert _core.ANALYSIS_FACT_SCHEMA == 40
    database = Path(os.environ["AXILE_CONTRACT_DB"])
    try:
        _core.migration_current(str(database), "{")
    except _core.NativeError as error:
        assert error.args[0] == "INVALID_REQUEST" and isinstance(error.args[1], str)
    else:
        raise AssertionError("native migration accepted malformed JSON")
    try:
        native_operation_sync("migration_current", {"public": float("nan")})
    except ValueError:
        pass
    else:
        raise AssertionError("native facade accepted non-finite JSON")
    assert not database.exists()
    for name in ("migration_current", "migration_check"):
        result = getattr(_core, name)(str(database), '{"public":"head"}')
        assert result["public"] is None and result["private"] is None
        assert result["public_target"] == "0039"
        assert not database.exists(), f"{name} created a missing database"

    initialize_database()
    assert _core.migration_current(str(database), "{}")["public"] == "0039"
    with closing(sqlite3.connect(database)) as oracle:
        assert oracle.execute("SELECT version_num FROM alembic_version").fetchall() == [("0039",)]
        journal_mode = oracle.execute("PRAGMA journal_mode").fetchone()
        assert journal_mode == ("wal",), journal_mode
        before = list(oracle.iterdump())
    initialize_database()
    _core.migration_upgrade(str(database), "{}")
    with closing(sqlite3.connect(database)) as oracle:
        assert list(oracle.iterdump()) == before
    _core.migration_downgrade(str(database), '{"public":"0036"}')
    assert _core.migration_current(str(database), "{}")["public"] == "0036"
    _core.migration_upgrade(str(database), "{}")
    assert _core.migration_current(str(database), "{}")["public"] == "0039"
    plan = _core.migration_check(str(database), '{"public":"head"}')
    assert plan["public_fields"] == plan["private_fields"] == 0
    json.dumps(plan, allow_nan=False)


def test_native_initialization_and_historical_roundtrip(tmp_path: Path) -> None:
    """新库、重复初始化和历史版本往返均走原生入口。"""
    _run_isolated(tmp_path, _assert_native_migrations)


def _assert_migration_rollback() -> None:
    import os
    import sqlite3
    from contextlib import closing

    from axile.server._native import _core

    database = os.environ["AXILE_CONTRACT_DB"]
    _core.migration_upgrade(database, '{"public":"0012"}')
    with closing(sqlite3.connect(database)) as oracle, oracle:
        oracle.execute(
            "CREATE TRIGGER reject_revision BEFORE UPDATE ON alembic_version "
            "WHEN NEW.version_num != '0012' BEGIN SELECT RAISE(ABORT,'contract rollback'); END"
        )
    with closing(sqlite3.connect(database)) as oracle:
        before = list(oracle.iterdump())
    try:
        _core.migration_upgrade(database, "{}")
    except _core.NativeError as error:
        assert len(error.args) == 2 and "contract rollback" in error.args[1]
    else:
        raise AssertionError("injected migration failure was ignored")
    with closing(sqlite3.connect(database)) as oracle:
        assert list(oracle.iterdump()) == before
    assert _core.migration_current(database, "{}")["public"] == "0012"


def test_native_upgrade_failure_preserves_schema_and_revision(tmp_path: Path) -> None:
    """升级中断时完整保留原 schema 与版本。"""
    _run_isolated(tmp_path, _assert_migration_rollback)


def _assert_missing_native_fails_closed() -> None:
    import os
    from pathlib import Path
    from unittest.mock import patch

    from axile.server._native import _core
    from axile.server.core.db import initialize_database

    with patch.object(_core, "migration_upgrade", None):
        try:
            initialize_database()
        except (AttributeError, TypeError, RuntimeError):
            pass
        else:
            raise AssertionError("startup accepted a missing native migration entrypoint")
    assert not Path(os.environ["AXILE_CONTRACT_DB"]).exists()


def test_missing_native_migration_has_no_python_fallback(tmp_path: Path) -> None:
    """缺少原生迁移能力时启动失败且不创建数据库。"""
    _run_isolated(tmp_path, _assert_missing_native_fails_closed)


def _assert_api_contract() -> None:
    import json
    from unittest.mock import AsyncMock, patch

    from fastapi.testclient import TestClient

    from axile.channels import get_channel
    from axile.server._native import _core
    from axile.server.app import _app
    from axile.server.core.db import analysis_store, initialize_database

    initialize_database()
    client = TestClient(_app)
    portfolio_fields = {
        "name",
        "market",
        "description",
        "custom_calc_py_code",
        "status",
        "tag",
        "id",
        "updated_at",
        "created_at",
        "account_id",
    }
    code = "def calculate_portfolio(context):\n    return {'rb2610': 1.0}\n"
    portfolio = client.post(
        "/api/v1/portfolio/", json={"name": "contract", "market": "ctp", "custom_calc_py_code": code}
    )
    assert portfolio.status_code == 201, portfolio.text
    assert set(portfolio.json()) == portfolio_fields
    portfolio_id = portfolio.json()["id"]
    assert client.get(f"/api/v1/portfolio/{portfolio_id}").json() == portfolio.json()
    assert client.get("/api/v1/portfolio/2147483647").json() == {"detail": "组合不存在"}
    invalid = client.patch(f"/api/v1/portfolio/{portfolio_id}", json={"custom_calc_py_code": " "})
    assert invalid.status_code == 422
    assert invalid.json()["detail"][0]["loc"] == ["body", "custom_calc_py_code"]
    assert client.get(f"/api/v1/portfolio/{portfolio_id}").json()["custom_calc_py_code"] == code

    defaults = get_channel("tq").descriptor.defaults
    credentials = {"account_mode": "sim", "tq_username": "contract-user", "tq_password": "contract-secret"}
    payload = {
        "name": "contract",
        "market": "ctp",
        "trade_channel": "tq",
        "account_control_preset": "default",
        "account_config": credentials,
        "is_started": False,
        "cron_expr": "30 9 * * *",
        "remark": None,
        "brokerage": "",
        "weight_precision": 0.01,
        "algorithm": defaults.trade_algorithm.model_dump(mode="json"),
        "empty_positions_algorithm": None,
        "trade_rules": None,
        "forbidden_symbols": None,
        "risk_symbols": None,
        "feishu_key": None,
        "portfolio_id": portfolio_id,
    }
    with (
        patch("socket.socket.connect", side_effect=AssertionError("contract test attempted network access")),
        patch("axile.server.execution.account_runtime_sync.reconcile_china_channel_account", new=AsyncMock()),
    ):
        created = client.post("/api/v1/account/", json=payload, headers={"Idempotency-Key": "contract-create"})
        assert created.status_code == 201, created.text
        account = created.json()
        expected_fields = set(
            "backtest_weight_type backtest_fee_rate id copied_from_account_id copied_from_account_name name market "
            "trade_channel account_control_preset account_control_override account_configured connection_values "
            "is_started cron_expr supplement has_pending_supplement remark brokerage weight_precision long_leverage "
            "short_leverage algorithm empty_positions_algorithm trade_rules futures_close_policy forbidden_symbols "
            "risk_symbols feishu_configured execution_notification_code portfolio_id write_empty_record execution_timeout "
            "updated_at created_at runtime_sync notification_state connection execution_notification_status".split()
        )
        assert set(account) == expected_fields
        assert account["account_configured"] and not account["feishu_configured"]
        assert account["connection_values"]["tq_password"] == credentials["tq_password"]
        account_id = account["id"]
        repeated = client.post("/api/v1/account/", json=payload, headers={"Idempotency-Key": "contract-create"})
        assert repeated.status_code == 201 and repeated.json()["id"] == account_id
        updated = client.patch(
            f"/api/v1/account/{account_id}", json={"account_config": {**credentials, "tq_password": "replacement"}}
        )
        assert updated.status_code == 200, updated.text
        fetched = client.get(f"/api/v1/account/{account_id}")
        assert fetched.status_code == 200 and set(fetched.json()) == expected_fields
        assert fetched.json()["connection_values"]["tq_password"] == "replacement"
        assert "contract-secret" not in fetched.text
        assert "account_config" not in fetched.json() and "feishu_key" not in fetched.json()
        accounts = client.get("/api/v1/account/")
        assert accounts.status_code == 200 and len(accounts.json()["data"]) == 1
        missing = client.get("/api/v1/account/2147483647")
        assert missing.status_code == 404 and missing.json() == {"detail": "账户不存在"}
        unknown = client.patch(f"/api/v1/account/{account_id}", json={"unknown": True})
        assert unknown.status_code == 422 and unknown.json()["detail"][0]["type"] == "extra_forbidden"
        for query in (_core.execution_records, _core.asset_snapshots):
            result = query(analysis_store(), account_id, 0, 1)
            assert isinstance(result, bytes) and json.loads(result)["data"] == []
    client.close()


def test_native_api_fields_errors_credentials_and_minimal_queries(tmp_path: Path) -> None:
    """真实存储保留账户与组合的公开读写契约。"""
    _run_isolated(tmp_path, _assert_api_contract)


def _assert_api_schema(baseline_path: str) -> None:
    import json
    from pathlib import Path

    from axile.server.app import _app

    def contract(value, *, property_map=False):
        if isinstance(value, dict):
            documentation = {"title", "description", "summary", "operationId", "tags", "example", "examples"}
            return {
                key: contract(item, property_map=key in {"properties", "schemas", "$defs"})
                for key, item in value.items()
                if property_map or key not in documentation
            }
        if isinstance(value, list):
            return [contract(item) for item in value]
        return value

    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    assert contract(_app.openapi()) == contract(baseline)


def test_public_api_schema_matches_frozen_baseline(tmp_path: Path) -> None:
    """比较冻结的 API 类型、字段、约束与错误响应。"""
    baseline = os.environ.get("AXILE_ACCEPTANCE_OPENAPI_BASELINE")
    if baseline is None:
        pytest.skip("set AXILE_ACCEPTANCE_OPENAPI_BASELINE to the frozen pre-migration OpenAPI JSON")
    _run_isolated(tmp_path, _assert_api_schema, str(Path(baseline).resolve()))
