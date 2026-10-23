"""账户原生读取与当前组合绑定契约。"""

import asyncio
from contextlib import closing

import pytest
from fastapi import HTTPException

from axile.server.api.routes.portfolio import delete_portfolio
from axile.server.core import db
from axile.server.db.models import AccountSnapshot
from axile.server.repositories import (
    get_account,
    get_account_snapshot,
    get_latest_account_id_by_portfolio_id,
    get_latest_portfolio_id_by_account_id,
)
from tests.conftest import native_store_call
from tests.sqlite_oracle import connect as oracle_connect
from tests.unit.server.test_account_control_routes import _build_account


def test_latest_binding_respects_explicit_unbinding(native_database, monkeypatch) -> None:
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    with closing(oracle_connect(native_database)) as connection, connection:
        connection.execute(
            "INSERT INTO portfolio(id,name,market,custom_calc_py_code,created_at,updated_at) VALUES(1,'p','期货',?,'now','now')",
            ("def calculate_portfolio(context): return {}",),
        )
    for name in ("first", "second"):
        native_store_call(
            native_database,
            "account_create",
            {
                "account": _build_account(name=name, portfolio_id=None).model_dump(mode="json"),
                "now": "2026-10-23T09:00:00",
            },
        )
    with closing(oracle_connect(native_database)) as connection, connection:
        connection.executemany(
            "INSERT INTO portfolioaccount(account_id,portfolio_id,created_at) VALUES(?,?,'now')",
            [(1, 1), (2, 1), (1, None)],
        )

    async def scenario():
        assert await get_latest_account_id_by_portfolio_id(1) == 2
        assert await get_latest_portfolio_id_by_account_id(1) is None
        with pytest.raises(HTTPException) as exc:
            await delete_portfolio(1)
        assert exc.value.status_code == 422
        with closing(oracle_connect(native_database)) as connection, connection:
            connection.execute("INSERT INTO portfolioaccount(account_id,portfolio_id,created_at) VALUES(2,NULL,'now')")
        assert await get_latest_account_id_by_portfolio_id(1) is None
        await delete_portfolio(1)
        with pytest.raises(HTTPException) as exc:
            await delete_portfolio(1)
        assert exc.value.status_code == 404

    asyncio.run(scenario())


def test_account_read_hydrates_independent_snapshot_and_missing(native_database, monkeypatch) -> None:
    monkeypatch.setattr(db, "database_path", lambda: str(native_database))
    native_store_call(
        native_database,
        "account_create",
        {
            "account": _build_account(portfolio_id=None).model_dump(mode="json"),
            "now": "2026-10-23T09:00:00",
        },
    )

    async def scenario():
        account = await get_account(1)
        snapshot = await get_account_snapshot(1)
        assert isinstance(snapshot, AccountSnapshot)
        assert account.runtime_sync.revision == 1
        snapshot.account_config["password"] = "changed"
        assert account.account_config["password"] == "test"
        assert (await get_account_snapshot(1)).account_config["password"] == "test"
        assert await get_account(999) is None
        assert await get_account_snapshot(999) is None

    asyncio.run(scenario())
