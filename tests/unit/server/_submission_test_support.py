"""报撤测试使用真实迁移和账户创建入口，不依赖 ORM 建表。"""

from tests.unit.server._execution_test_support import build_account


def initialize_submission_database(database, monkeypatch):
    from tests.conftest import initialize_native_database, native_store_call

    initialize_native_database(database)
    monkeypatch.setattr("axile.server.core.db.database_path", lambda: database)
    for account_id in (1, 2):
        result = native_store_call(
            database,
            "account_create",
            {
                "account": build_account(id=account_id, portfolio_id=None).model_dump(mode="json"),
                "now": "2026-10-19T08:00:00",
                "idempotency_key": None,
            },
        )
        assert result["account"]["id"] == account_id
