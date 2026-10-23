"""冻结 Python 编码契约，供 Rust 终态写入与 0037 迁移共用。"""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from axile.server.business_facts import orders, trades

_FIXTURE = Path(__file__).parents[2] / "fixtures/rust_store/result_orders_trades.json"


@pytest.mark.parametrize("domain", [orders, trades])
def test_result_codec_matches_frozen_native_fixture(domain):
    frozen = json.loads(_FIXTURE.read_text())
    name = domain.__name__.rsplit(".", 1)[1]
    batch = getattr(domain, f"encode_{name}")(frozen["record_id"], frozen["payload"])
    rows = {table.name: values for table, values in batch.items()}

    assert rows == frozen[name]
    assert getattr(domain, f"decode_{name}")(rows) == frozen[f"decoded_{name}"]


@pytest.mark.parametrize("domain", [orders, trades])
def test_sql_result_matches_frozen_native_fixture(domain):
    frozen = json.loads(_FIXTURE.read_text())
    name = domain.__name__.rsplit(".", 1)[1]
    engine = sa.create_engine("sqlite://")
    try:
        raw = engine.raw_connection()
        try:
            raw.executescript(frozen["schema"])
        finally:
            raw.close()
        with engine.begin() as connection:
            for table in domain.TABLES:
                values = frozen[name][table.name]
                if values:
                    connection.execute(table.insert(), values)
            rows = {table.name: connection.execute(sa.select(table)).mappings().all() for table in domain.TABLES}
            assert getattr(domain, f"decode_{name}")(rows) == frozen[f"decoded_sql_{name}"]
    finally:
        engine.dispose()
