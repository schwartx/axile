"""0034 时间字段的真实迁移契约；JSON 保持原始存储。"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.parametrize("host_timezone", ["UTC", "Asia/Shanghai", "America/New_York"])
def test_naive_historical_scalars_use_migration_host_timezone(tmp_path, host_timezone):
    source = Path(__file__).parents[2] / "fixtures/rust_store/v0.23.1.sql"
    database = tmp_path / "local-time.db"
    program = r"""
import json, sqlite3, sys, time
from axile.server.native_store import native_operation_sync
from tests.fixtures.rust_store.schema_contract import load_schema
if hasattr(time, "tzset"):
    time.tzset()
with sqlite3.connect(sys.argv[1]) as connection:
    load_schema(connection, open(sys.argv[2]).read())
    connection.execute("INSERT INTO alembic_version VALUES ('0033')")
    connection.execute("INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) VALUES(1,'time','FUTURES','ctp','','2026-07-01T09:00:00.123456','2026-07-01T09:00:00.123456')")
    connection.execute("INSERT INTO account(id,name,market,trade_channel,brokerage,created_at,updated_at) VALUES(2,'negative','FUTURES','ctp','','1969-12-31T23:59:59.999999','1969-12-31T23:59:59.999999')")
    connection.execute("CREATE TABLE user_opaque(id INTEGER PRIMARY KEY,payload JSON)")
    connection.execute("INSERT INTO user_opaque VALUES(1,?)", ('{ "created_at" : "not-a-date", "nested":{"trade_time":"2026-07-01T09:00:00"}}',))
receipt = native_operation_sync("migration_upgrade", {}, database=sys.argv[1])
with sqlite3.connect(sys.argv[1]) as connection:
    instant, kind = connection.execute("SELECT created_at_us,typeof(created_at_us) FROM account WHERE id=1").fetchone()
    negative = connection.execute("SELECT created_at_us FROM account WHERE id=2").fetchone()[0]
    opaque = connection.execute("SELECT payload FROM user_opaque").fetchone()[0]
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
print(json.dumps({"instant":instant,"kind":kind,"negative":negative,"opaque":opaque,"integrity":integrity,"revision":receipt["public"]}))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", program, str(database), str(source)],
        env={**os.environ, "TZ": host_timezone},
        cwd=Path(__file__).parents[3],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    instant = datetime(2026, 7, 1, 9, 0, 0, 123456, tzinfo=ZoneInfo(host_timezone)).astimezone(UTC)
    delta = instant - datetime(1970, 1, 1, tzinfo=UTC)
    assert observed["instant"] == (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    previous = datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=ZoneInfo(host_timezone)).astimezone(UTC)
    previous_delta = previous - datetime(1970, 1, 1, tzinfo=UTC)
    assert (
        observed["negative"]
        == (previous_delta.days * 86400 + previous_delta.seconds) * 1_000_000 + previous_delta.microseconds
    )
    assert observed["kind"] == "integer"
    assert observed["opaque"] == '{ "created_at" : "not-a-date", "nested":{"trade_time":"2026-07-01T09:00:00"}}'
    assert observed["revision"] == "0034"
    assert observed["integrity"] == "ok"
