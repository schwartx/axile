from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from tests.conftest import initialize_native_database, native_store_call, require_native_store

_OLD_DEFAULT_CODE = '''import os

from axile.common.feishu import push_feishu_card
from axile.common.notification_context import AccountNotificationContext


def notify(context: AccountNotificationContext) -> None:
    """发送与内置默认通知一致的飞书卡片。"""
    key = os.environ.get("AXILE_ACCOUNT_FEISHU_KEY")
    if not key:
        return

    # 保留原默认卡片的口径；可以在这里修改字段、持仓或成交的展示。
    defaults = context["default_feishu_variables"]
    variables = {
        "account_mark": defaults["account_mark"],
        "dt": defaults["dt"],
        "algorithm": defaults["algorithm"],
        "total_assets": defaults["total_assets"],
        "available_cash": defaults["available_cash"],
        "market_value": defaults["market_value"],
        "positions": defaults["positions"],
        "trades": defaults["trades"],
    }
    card = {
        "type": "template",
        "data": {
            "template_id": "AAqRUQhyOM90g",
            "template_variable": variables,
        },
    }
    push_feishu_card(card, key, timeout=10)
'''


@contextmanager
def _connection(path: Path) -> Iterator[sqlite3.Connection]:
    # 两套 SQLite 库共用文件时，必须在进入 Rust 前关闭 Python 连接。
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        yield connection


def _insert_account(connection: sqlite3.Connection, **overrides: object) -> None:
    values = {
        "name": "account",
        "market": "future",
        "trade_channel": "gm",
        "account_control_preset": "default",
        "account_config": {},
        "is_started": False,
        "cron_expr": "",
        "brokerage": "",
        "weight_precision": 0.001,
        "updated_at": "now",
        "created_at": "now",
        **overrides,
    }
    connection.execute(
        f"INSERT INTO account ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
        tuple(json.dumps(value) if isinstance(value, (dict, list)) else value for value in values.values()),
    )


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row["name"]: row for row in connection.execute(f"PRAGMA table_info({table})")}


def _index_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA index_list({table})")}


def test_migration_history_has_one_root_and_head(tmp_path: Path) -> None:
    """原生迁移链应从唯一根节点逐版连通到唯一 head，且无断链或循环。"""
    core = require_native_store("migration_current", "migration_check", "migration_upgrade")
    path = tmp_path / "history.db"
    assert native_store_call(path, "migration_current", {})["public"] is None
    assert native_store_call(path, "migration_check", {})["public"] is None
    assert not path.exists()
    revisions = [f"{number:04}" for number in range(1, int(core.PUBLIC_HEAD) + 1)]
    visited = []
    for revision in revisions:
        visited.append(initialize_native_database(path, revision=revision)["public"])
        assert native_store_call(path, "migration_current", {})["public"] == revision
    assert len(visited) == len(set(visited))
    assert visited == revisions
    assert visited[-1] == core.PUBLIC_HEAD == "0039"


def test_feishu_card_config_migration_adds_nullable_account_column(tmp_path: Path) -> None:
    """历史账户迁移后应继续以空配置使用 Axon 默认卡片。"""
    path = tmp_path / "card-config.db"
    initialize_native_database(path, revision="0007")
    with _connection(path) as connection:
        _insert_account(connection, id=1)
    initialize_native_database(path, revision="0008")
    with _connection(path) as connection:
        assert _columns(connection, "account")["feishu_card_config"]["notnull"] == 0
        assert connection.execute("SELECT feishu_card_config FROM account WHERE id = 1").fetchone()[0] is None


def test_notification_function_migration_discards_old_card_config(tmp_path: Path) -> None:
    """旧模板与 JSON 值失效，账户获得默认模式与唯一函数槽位。"""
    path = tmp_path / "notification-functions.db"
    initialize_native_database(path, revision="0016")
    with _connection(path) as connection:
        _insert_account(connection, id=1, feishu_card_config={})
    initialize_native_database(path, revision="0017")
    with _connection(path) as connection:
        columns = _columns(connection, "account")
        assert "feishu_card_config" not in columns
        assert {"execution_notification_mode", "execution_notification_code"} <= columns.keys()
        row = connection.execute(
            "SELECT execution_notification_mode, execution_notification_code FROM account WHERE id = 1"
        ).fetchone()
        assert tuple(row) == ("default", None)


def test_single_account_notification_migration_preserves_enabled_notifications(tmp_path: Path) -> None:
    """默认飞书、自定义函数、未配置三种旧账户按实际通知行为迁移。"""
    from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE

    path = tmp_path / "single-notification.db"
    initialize_native_database(path, revision="0017")
    with _connection(path) as connection:
        for account_id, key, mode, code in (
            (1, "hook", "default", None),
            (2, None, "function", "def notify(context): pass"),
            (3, None, "default", None),
            (4, None, "default", "def notify(context): pass"),
        ):
            _insert_account(
                connection,
                id=account_id,
                feishu_key=key,
                execution_notification_mode=mode,
                execution_notification_code=code,
            )
    initialize_native_database(path, revision="0018")
    with _connection(path) as connection:
        assert "execution_notification_mode" not in _columns(connection, "account")
        rows = [row[0] for row in connection.execute("SELECT execution_notification_code FROM account ORDER BY id")]
        assert rows == [DEFAULT_ACCOUNT_NOTIFICATION_CODE, "def notify(context): pass", None, None]
    native_store_call(path, "migration_downgrade", {"public": "0017"})
    with _connection(path) as connection:
        restored = connection.execute(
            "SELECT execution_notification_mode, execution_notification_code FROM account ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in restored] == [
            ("default", None),
            ("function", "def notify(context): pass"),
            ("default", None),
            ("default", None),
        ]


def test_default_notification_type_migration_updates_only_unmodified_templates(tmp_path: Path) -> None:
    """旧默认函数自动修正类型标注，自定义源码保持原样。"""
    from axile.common.default_account_notification import DEFAULT_ACCOUNT_NOTIFICATION_CODE

    path = tmp_path / "notification-type.db"
    initialize_native_database(path, revision="0018")
    with _connection(path) as connection:
        for account_id, code in ((1, _OLD_DEFAULT_CODE), (2, "def notify(context): pass"), (3, None)):
            _insert_account(connection, id=account_id, execution_notification_code=code)
    initialize_native_database(path, revision="0019")
    with _connection(path) as connection:
        rows = [row[0] for row in connection.execute("SELECT execution_notification_code FROM account ORDER BY id")]
        assert rows == [DEFAULT_ACCOUNT_NOTIFICATION_CODE, "def notify(context): pass", None]


def test_account_asset_snapshot_migration_backfills_execution_assets(tmp_path: Path) -> None:
    path = tmp_path / "asset-snapshot.db"
    initialize_native_database(path, revision="0003")
    with _connection(path) as connection:
        _insert_account(connection, id=1)
        connection.executemany(
            "INSERT INTO executerecord (account_id, execution_id, raw_input, raw_result, is_success, created_at) "
            "VALUES (1, ?, '{}', ?, 1, ?)",
            (
                (
                    "exec-1",
                    json.dumps({"account_assets": {"total_asset": 123.0, "positions": []}}),
                    "2026-08-24T09:30:00",
                ),
                ("exec-2", json.dumps({"error": "before snapshot"}), "2026-08-24T09:31:00"),
            ),
        )
    initialize_native_database(path, revision="0004")
    with _connection(path) as connection:
        rows = connection.execute("SELECT * FROM account_asset_snapshot").fetchall()
        assert len(rows) == 1
        assert rows[0]["account_id"] == 1
        assert rows[0]["execution_id"] == "exec-1"
        assert rows[0]["source"] == "execution"


def test_target_weight_snapshot_migration_backfills_latest_account_target(tmp_path: Path) -> None:
    path = tmp_path / "target-snapshot.db"
    initialize_native_database(path, revision="0004")
    with _connection(path) as connection:
        connection.execute(
            "INSERT INTO portfolio (id, name, market, custom_calc_py_code, updated_at, created_at) "
            "VALUES (7, 'portfolio', 'future', '', 'now', 'now')"
        )
        _insert_account(connection, id=1)
        connection.execute(
            "INSERT INTO portfolioaccount (id, account_id, portfolio_id, created_at) "
            "VALUES (1, 1, 7, '2026-08-24T09:00:00')"
        )
        connection.executemany(
            "INSERT INTO executerecord (id, account_id, execution_id, raw_input, raw_result, is_success, created_at) "
            "VALUES (?, 1, ?, ?, '{}', 1, ?)",
            (
                (1, "exec-old", json.dumps({"curr_target": {"rb2610": 0.5}}), "2026-08-24T09:30:00"),
                (2, "exec-new", json.dumps({"curr_target": {"rb2610": 1.5}}), "2026-08-24T10:30:00"),
            ),
        )
    initialize_native_database(path, revision="0005")
    with _connection(path) as connection:
        rows = connection.execute("SELECT * FROM target_weight_snapshot").fetchall()
        assert len(rows) == 1
        assert rows[0]["portfolio_id"] == 7
        assert rows[0]["account_id"] == 1
        assert rows[0]["raw_weights"] in (None, "null")
        assert rows[0]["normalized_weights"] == '{"rb2610": 1.5}'
        assert rows[0]["execution_id"] == "exec-new"


def test_target_weight_snapshot_migration_adopts_existing_model_table(tmp_path: Path) -> None:
    """历史模型提前建表时，迁移应补索引并保持回填幂等。"""
    path = tmp_path / "existing-target.db"
    initialize_native_database(path, revision="0004")
    with _connection(path) as connection:
        connection.execute(
            "CREATE TABLE target_weight_snapshot (id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL, "
            "account_id INTEGER, raw_weights JSON, normalized_weights JSON, source TEXT NOT NULL, "
            "execution_id TEXT, calculated_at TEXT NOT NULL)"
        )
    initialize_native_database(path, revision="0005")
    initialize_native_database(path, revision="0005")
    with _connection(path) as connection:
        assert {
            "ix_target_weight_snapshot_portfolio_id_id",
            "ix_target_weight_snapshot_account_id_id",
            "ix_target_weight_snapshot_execution_id",
        } == _index_names(connection, "target_weight_snapshot")


def test_initial_baseline_creates_the_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "baseline.db"
    initialize_native_database(path, revision="0001")
    with _connection(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables - {"alembic_version"} == {
            "account",
            "account_control_counter_delta",
            "account_control_event",
            "executerecord",
            "execution_artifact",
            "execution_event",
            "portfolio",
            "portfolioaccount",
            "schedule_skip",
        }
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0001"
        for table_name, column_name in (
            ("account", "trade_channel"),
            ("execution_event", "channel"),
            ("account_control_event", "channel"),
        ):
            columns = _columns(connection, table_name)
            assert columns[column_name]["type"] == "TEXT"
            assert columns[column_name]["notnull"] == 1
        assert {
            "ix_executerecord_execution_id",
            "ix_executerecord_account_id_id",
            "ix_executerecord_account_id_created_at",
            "ix_executerecord_account_id_success_id",
        } <= _index_names(connection, "executerecord")
        assert {
            "ix_account_control_event_execution_seq",
            "ix_account_control_event_account_operation_occurred",
            "ix_account_control_event_account_symbol_operation_occurred",
        } <= _index_names(connection, "account_control_event")
        indexes = {row["name"]: row for row in connection.execute("PRAGMA index_list(executerecord)")}
        assert indexes["ix_executerecord_execution_id"]["unique"] == 1
        assert any(
            row["table"] == "portfolio" and row["from"] == "portfolio_id" and row["on_delete"] == "SET NULL"
            for row in connection.execute("PRAGMA foreign_key_list(account)")
        )
        event_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'execution_event'"
        ).fetchone()[0]
        assert "CONSTRAINT uq_execution_event_event_uid UNIQUE (event_uid)" in event_sql
        assert "SCHEDULE_SKIPPED" not in event_sql
        assert any(
            row["table"] == "account" and row["from"] == "account_id" and row["on_delete"] == "CASCADE"
            for row in connection.execute("PRAGMA foreign_key_list(schedule_skip)")
        )
        assert "ix_schedule_skip_account_triggered" in _index_names(connection, "schedule_skip")
        assert _columns(connection, "portfolio")["custom_calc_py_code"]["notnull"] == 1


def test_trading_calendar_migration_adds_calendar_table(tmp_path: Path) -> None:
    path = tmp_path / "calendar.db"
    initialize_native_database(path, revision="0001")
    initialize_native_database(path, revision="0002")
    with _connection(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"trading_calendar", "trading_calendar_override", "trading_calendar_config"} <= tables
        for table, primary_key in (
            ("trading_calendar", ["calendar_id", "cal_date"]),
            ("trading_calendar_override", ["calendar_id", "cal_date"]),
            ("trading_calendar_config", ["calendar_id"]),
        ):
            columns = _columns(connection, table)
            assert [
                name for name in sorted(columns, key=lambda name: columns[name]["pk"]) if columns[name]["pk"]
            ] == primary_key
        assert set(_columns(connection, "trading_calendar_config")) == {
            "calendar_id",
            "refresh_kind",
            "function_code",
            "last_sync_at",
            "updated_at",
        }


def test_drop_trading_calendar_migration_preserves_schedule_history(tmp_path: Path) -> None:
    path = tmp_path / "drop-calendar.db"
    initialize_native_database(path, revision="0002")
    initialize_native_database(path, revision="0006")
    with _connection(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "schedule_skip" in tables
        assert "trading_calendar" not in tables
        assert "trading_calendar_override" not in tables
        assert "trading_calendar_config" not in tables
    native_store_call(path, "migration_downgrade", {"public": "0005"})
    with _connection(path) as connection:
        restored = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"trading_calendar", "trading_calendar_override", "trading_calendar_config"} <= restored


def test_plugin_network_migration_rewrites_account_config_and_downgrades(tmp_path: Path) -> None:
    path = tmp_path / "plugin-network.db"
    initialize_native_database(path, revision="0002")
    with _connection(path) as connection:
        for account_id, channel, config in (
            (1, "plugin-channel", {"is_testnet": True}),
            (2, "plugin-channel", {"is_testnet": False}),
            (3, "plugin-channel", {"is_testnet": True, "network": "mainnet"}),
            (4, "gm", {"is_testnet": True}),
        ):
            _insert_account(connection, id=account_id, trade_channel=channel, account_config=config)
    initialize_native_database(path, revision="0003")
    with _connection(path) as connection:
        upgraded = {row[0]: json.loads(row[1]) for row in connection.execute("SELECT id, account_config FROM account")}
        assert upgraded[1] == {"network": "testnet"}
        assert upgraded[2] == {"network": "mainnet"}
        assert upgraded[3] == {"network": "mainnet"}
        assert upgraded[4] == {"is_testnet": True}
    native_store_call(path, "migration_downgrade", {"public": "0002"})
    with _connection(path) as connection:
        downgraded = {
            row[0]: json.loads(row[1]) for row in connection.execute("SELECT id, account_config FROM account")
        }
        assert downgraded[1] == {"is_testnet": True}
        assert downgraded[2] == {"is_testnet": False}
        assert downgraded[3] == {"is_testnet": False}
        assert downgraded[4] == {"is_testnet": True}


def test_ctp_preset_migration_only_updates_ctp_default_and_preserves_override(tmp_path: Path) -> None:
    path = tmp_path / "ctp-preset.db"
    initialize_native_database(path, revision="0008")
    override = {"groups": {"ctp_td_global": {"min_interval_ms": {"limit": 2000}}}}
    with _connection(path) as connection:
        for account_id, name, channel, preset, control_override in (
            (1, "ctp-default", "ctp", "default", override),
            (2, "ctp-explicit", "ctp", "custom", None),
            (3, "gm-default", "gm", "default", None),
        ):
            _insert_account(
                connection,
                id=account_id,
                name=name,
                trade_channel=channel,
                account_control_preset=preset,
                account_control_override=control_override,
            )
    initialize_native_database(path, revision="0009")
    with _connection(path) as connection:
        rows = [
            (row[0], row[1], json.loads(row[2]) if row[2] is not None else None)
            for row in connection.execute(
                "SELECT id, account_control_preset, account_control_override FROM account ORDER BY id"
            )
        ]
        assert rows == [(1, "ctp", override), (2, "custom", None), (3, "default", None)]
    native_store_call(path, "migration_downgrade", {"public": "0008"})
    with _connection(path) as connection:
        assert connection.execute("SELECT account_control_preset FROM account WHERE id = 1").fetchone()[0] == "ctp"
