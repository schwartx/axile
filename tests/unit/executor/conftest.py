"""每个测试隔离进程级目录；一个测试内仍可验证跨账户共享。"""

import pytest

from axile.executor import instrument_catalog


@pytest.fixture(autouse=True)
def isolate_process_instrument_catalog(monkeypatch):
    catalog = instrument_catalog.InstrumentCatalog()
    monkeypatch.setattr(instrument_catalog, "LOCAL_INSTRUMENT_CATALOG", catalog)
    yield
    catalog.close()
