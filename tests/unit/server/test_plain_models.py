"""普通服务端模型的配置、公开字段与无 ORM 导入契约。"""

import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from axile.common.serialization import from_payload, parse_input, to_payload
from axile.domain.execution import ExecutionArtifactType, ExecutionEventType
from axile.server.db.models import (
    Account,
    AccountCreate,
    AccountSnapshot,
    AccountUpdate,
    ExecutionArtifact,
    ExecutionEvent,
    Portfolio,
    PortfolioCreate,
    PortfolioUpdate,
)
from tests.fixtures.rust_store.execution_rows import ExecuteRecord


def account_data() -> dict:
    return {
        "id": 1,
        "name": "账户",
        "market": "期货",
        "trade_channel": "ctp",
        "brokerage": "test",
        "account_config": {"nested": {"secret": "test-only"}},
        "algorithm": {"method": "SINGLE-MAKER", "params": {}},
        "is_started": True,
        "cron_expr": "0 9 * * *",
        "feishu_key": "test-key",
    }


def test_models_import_without_python_database_dependencies():
    project = Path(__file__).resolve().parents[3]
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
from pathlib import Path
from dataclasses import fields

class RejectDatabasePackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sqlalchemy', 'sqlmodel', 'alembic', 'aiosqlite', 'sqlite3', 'pydantic', 'pydantic_core', 'pydantic_settings'}:
            raise AssertionError(fullname)
        return None

sys.meta_path.insert(0, RejectDatabasePackages())
import axile
from axile.server.db import models
assert Path(axile.__file__).resolve().is_relative_to(Path.cwd())
assert not hasattr(models.Account, '__table__')
assert not hasattr(models.Account, 'snapshot')
assert not hasattr(models, 'SQLModel')
assert not hasattr(models, 'PydanticJSONType')
""",
        ],
        cwd=project,
        timeout=30,
        check=True,
        capture_output=True,
        text=True,
    )


def test_native_account_hydrates_flat_configuration_and_states():
    data = account_data() | {
        "has_pending_supplement": True,
        "notification_state": {"account_id": 1, "last_attempt_ok": False},
        "connection_state": {"account_id": 1, "config_key": "config", "sequence": 3},
        "runtime_sync": {"status": "pending", "revision": 2, "attempts": 0},
    }
    account = from_payload(Account, data)
    assert account.is_started
    assert account.weight_precision == 0.01
    assert account.account_config == data["account_config"]
    assert "settings" not in {item.name for item in fields(Account)}
    assert account.has_pending_supplement
    assert account.notification_state.last_attempt_ok is False
    assert account.connection_state.sequence == 3
    assert account.runtime_sync.revision == 2
    assert to_payload(account, include={"name", "feishu_key"}) == {"name": "账户", "feishu_key": "test-key"}
    assert not {"settings", "notification_state", "connection_state", "runtime_sync", "has_pending_supplement"} & (
        to_payload(account).keys()
    )


@pytest.mark.parametrize("account_id", [None, 2])
def test_account_preserves_native_id(account_id):
    account = from_payload(Account, account_data() | {"id": account_id})
    assert account.id == (int(account_id) if account_id is not None else None)


def test_account_snapshot_and_validated_patch_do_not_mutate_original():
    account = from_payload(Account, account_data())
    snapshot = from_payload(AccountSnapshot, to_payload(account))
    assert isinstance(snapshot, AccountSnapshot)
    snapshot.account_config["nested"]["secret"] = "changed"
    assert account.account_config["nested"]["secret"] == "test-only"
    before = to_payload(account)
    with pytest.raises(ValueError):
        parse_input(Account, before | {"long_leverage": 3, "cron_expr": "invalid"})
    assert to_payload(account) == before
    with pytest.raises(ValueError):
        parse_input(Account, before | {"long_leverage": 3, "account_config": None})
    assert to_payload(account) == before
    account.long_leverage = 3
    assert account.long_leverage == 3
    assert snapshot.long_leverage is None
    assert from_payload(Account, snapshot).account_config == snapshot.account_config


def test_public_models_read_attributes_without_exposing_credentials():
    account = from_payload(Account, account_data())
    from tests.unit.server._native_read_views import account_view

    public = account_view(account)
    assert public["is_started"] is True
    assert public["weight_precision"] == 0.01
    assert public["execution_notification_status"] == "none"
    assert "account_config" not in public
    assert "feishu_key" not in public
    assert "test-only" not in str(public)
    assert "test-key" not in str(public)


@pytest.mark.parametrize(
    "payload",
    [{"weight_precision": -1}, {"execution_timeout": 0}, {"execution_timeout": -1}, {"unexpected": True}],
)
def test_account_update_keeps_validation(payload):
    with pytest.raises(ValueError):
        parse_input(AccountUpdate, payload)


@pytest.mark.parametrize("timeout", [541, 3600, 86400])
def test_account_models_accept_long_execution_timeout(timeout):
    account = from_payload(Account, account_data() | {"execution_timeout": timeout})
    payload = to_payload(account, include={item.name for item in fields(AccountCreate)})
    assert from_payload(AccountCreate, payload).execution_timeout == timeout
    assert from_payload(AccountSnapshot, to_payload(account)).execution_timeout == timeout


def test_portfolio_preserves_source_and_rejects_clearing_code():
    source = "\ndef calculate_portfolio(context):\n    return {}\n"
    portfolio = Portfolio(name="组合", market="期货", custom_calc_py_code=source)
    public = to_payload(portfolio, mode="json")
    assert public["custom_calc_py_code"] == source
    assert public["description"] is None
    assert public["id"] is None
    assert portfolio.created_at
    assert to_payload(PortfolioUpdate(), exclude_unset=True) == {}
    for code in (None, "", " \n"):
        with pytest.raises(ValueError):
            parse_input(PortfolioUpdate, {"custom_calc_py_code": code})
    with pytest.raises(ValueError):
        parse_input(
            PortfolioCreate, {"name": "组合", "market": "期货", "custom_calc_py_code": source, "unexpected": True}
        )


def test_execution_records_preserve_zero_null_and_hidden_evidence():
    record = ExecuteRecord(account_id=1, is_success=0, success=False, technical_detail="internal")
    other = ExecuteRecord(account_id=1, is_success=0)
    public = to_payload(record, mode="json")
    assert record.execution_id != other.execution_id
    assert public["is_success"] == 0
    assert public["success"] is False
    assert public["status"] is None
    assert record.technical_detail == "internal"
    assert not {"facts_complete", "audit_evidence", "result_input_kind", "technical_detail"} & public.keys()


def test_execution_audit_enums_and_json_defaults():
    event_data = {
        "execution_id": "execution",
        "account_id": 1,
        "channel": "ctp",
        "algorithm": "test",
        "event_uid": "event",
        "event_type": "execution_started",
        "status": "INFO",
        "reason_family": "INPUT",
        "reason_code": "test",
    }
    event = from_payload(ExecutionEvent, event_data)
    assert event.event_type is ExecutionEventType.EXECUTION_STARTED
    assert to_payload(event, mode="json")["event_type"] == "execution_started"
    event.details["first"] = True
    assert from_payload(ExecutionEvent, event_data).details == {}
    artifact = ExecutionArtifact(execution_id="execution", artifact_type=ExecutionArtifactType.ENVIRONMENT_SNAPSHOT)
    assert artifact.artifact_type is ExecutionArtifactType.ENVIRONMENT_SNAPSHOT
    assert to_payload(artifact, mode="json")["artifact_type"] == "environment_snapshot"
    with pytest.raises(ValueError):
        parse_input(ExecutionArtifact, {"execution_id": "execution", "artifact_type": "execution_summary"})
