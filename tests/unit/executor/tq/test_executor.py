from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from axile.executor.instrument_catalog import InstrumentCatalog, LocalInstrumentCatalogProvider
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import TQAccountConfig
from axile.executor.models.unified_order import OrderDirection, OrderType
from axile.executor.tq import TQExecutor
from axile.executor.tq.tq_execute import TQTradingTimeCheck, TQTradingTimeStatus
from tests.unit.executor.tq._catalog_test_support import CatalogApiMixin


def test_injected_catalog_registers_existing_tq_connection_once(executor, monkeypatch):
    instance, _ = executor
    original = instance._instrument_provider
    catalog = InstrumentCatalog()
    provider = LocalInstrumentCatalogProvider(catalog)
    bind = Mock(wraps=provider.bind)
    monkeypatch.setattr(provider, "bind", bind)
    instance.set_instrument_catalog_provider(provider)
    instance.set_instrument_catalog_provider(provider)
    bind.assert_called_once()
    assert original.catalog.status()["sources"] == 0
    assert catalog.status()["sources"] == 1
    assert provider.ensure(["SHFE.rb2610"])["SHFE.rb2610"].multiplier is None


class FakeApi(CatalogApiMixin):
    def __init__(self) -> None:
        self.quote = {
            "exchange_id": "SHFE",
            "instrument_id": "rb2610",
            "last_price": 3200,
            "bid_price1": 3199,
            "ask_price1": 3201,
            "bid_volume1": 2,
            "ask_volume1": 3,
            "price_tick": 1,
            "volume_multiple": 10,
            "datetime": 1_700_000_000_000_000_000,
        }
        self.order = {
            "order_id": "o1",
            "exchange_id": "SHFE",
            "instrument_id": "rb2610",
            "direction": "BUY",
            "offset": "OPEN",
            "price_type": "LIMIT",
            "volume_orign": 2,
            "volume_left": 2,
            "limit_price": 3200,
            "status": "ALIVE",
        }
        self.trade = {
            "trade_id": "t1",
            "order_id": "o1",
            "exchange_id": "SHFE",
            "instrument_id": "rb2610",
            "direction": "BUY",
            "offset": "OPEN",
            "volume": 1,
            "price": 3200,
            "trade_date_time": 1_700_000_000_000_000_000,
        }
        self.insert_args: dict[str, object] = {}
        self.order_lookups: list[str] = []
        self.canceled: list[str] = []
        self.quote_calls = 0
        self.closed = False
        self.loop: asyncio.AbstractEventLoop | None = None

    def create_task(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task:
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
        return self.loop.create_task(coroutine)

    def wait_update(self, *, deadline: float) -> bool:
        del deadline
        if self.loop is not None:
            self.loop.run_until_complete(asyncio.sleep(0.001))
        return False

    def is_changing(self, _entity: object) -> bool:
        return False

    async def get_quote(self, _symbol: str) -> dict[str, object]:
        self.quote_calls += 1
        return self.quote

    def get_trading_calendar(self, start: date, end: date) -> object:
        rows = []
        current = start
        while current <= end:
            rows.append({"date": current, "trading": current.weekday() < 5})
            current += timedelta(days=1)
        return SimpleNamespace(to_dict=lambda _orient: rows)

    def get_account(self) -> dict[str, object]:
        return {"available": 900_000, "balance": 1_000_000}

    def get_position(self) -> dict[str, object]:
        return {
            "SHFE.rb2610": SimpleNamespace(
                pos_long_today=1,
                pos_long_his=2,
                pos_short_today=0,
                pos_short_his=0,
                volume_long_frozen=0,
                position_cost_long=30_000,
                open_price_long=3000,
            )
        }

    def insert_order(self, **kwargs: object) -> dict[str, object]:
        self.insert_args = kwargs
        self.order = {
            **self.order,
            "order_id": kwargs["order_id"],
            "direction": kwargs["direction"],
            "offset": kwargs["offset"],
            "volume_orign": kwargs["volume"],
            "volume_left": kwargs["volume"],
            "limit_price": kwargs["limit_price"] or 0,
        }
        return self.order

    def cancel_order(self, order_id: str) -> None:
        self.canceled.append(order_id)

    def get_order(self, order_id: str | None = None) -> dict[str, object]:
        if order_id is not None:
            self.order_lookups.append(order_id)
        return self.order if order_id else {"o1": self.order}

    def get_trade(self) -> dict[str, object]:
        return {"t1": self.trade}

    def close(self) -> None:
        if self.loop is not None:
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self.loop.close()
            asyncio.set_event_loop(None)
        self.closed = True


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> tuple[TQExecutor, FakeApi]:
    api = FakeApi()
    monkeypatch.setattr(TQExecutor, "_build_api", staticmethod(lambda _config: api))
    instance = TQExecutor(TQAccountConfig(account_mode="kq", tq_username="user", tq_password="secret"))
    try:
        yield instance, api
    finally:
        instance.close()


def test_executor_converts_queries_and_order_primitives(
    executor: tuple[TQExecutor, FakeApi], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, api = executor
    monkeypatch.setattr(
        instance,
        "_check_tq_symbol_trading_time",
        lambda _api, _sessions, _now: TQTradingTimeCheck(TQTradingTimeStatus.OPEN),
    )

    market = instance.get_market_data(["rb2610"])
    assets = instance.get_account_assets()
    order = instance._place_order_impl(
        "rb2610",
        OrderDirection.SELL,
        OrderType.LIMIT,
        2,
        3199,
        offset_flag="3",
    )

    assert market["rb2610"].last_price == 3200
    assert assets.positions[0].symbol == "rb2610"
    assert assets.positions[0].extra["long_yd"] == 2
    assert assets.positions[0].market_value == 3 * 3200 * 10
    assert assets.market_value == 3 * 3200 * 10
    assert assets.positions[0].extra["position_cost"] == 30_000
    assert assets.total_asset == 1_000_000
    assert assets.available_cash == 900_000
    assert order.symbol == "rb2610"
    assert {key: value for key, value in api.insert_args.items() if key != "order_id"} == {
        "symbol": "SHFE.rb2610",
        "direction": "SELL",
        "offset": "CLOSETODAY",
        "volume": 2,
        "limit_price": 3199.0,
    }
    native_order_id = api.insert_args["order_id"]
    assert isinstance(native_order_id, str) and len(native_order_id) == 32
    assert order.order_id == native_order_id
    assert api.order_lookups == []
    assert instance._cancel_order_impl("rb2610", "o1") is True
    assert api.canceled == ["o1"]
    assert instance._query_trades_impl("rb2610", "o1")[0].symbol == "rb2610"


def test_executor_uses_contract_multiplier_and_rejects_fractional_lots(
    executor: tuple[TQExecutor, FakeApi],
) -> None:
    instance, _api = executor
    assets = UnifiedAccountAssets(available_cash=1_000_000, total_asset=1_000_000, market_value=0, positions=[])

    assert instance.get_tick_size("rb2610") == 1
    assert instance._calculate_generic_volume(0.32, 3200, assets, {}, symbol="rb2610") == 10
    with pytest.raises(ValueError, match="正整数手"):
        instance._place_order_impl("rb2610", OrderDirection.BUY, OrderType.LIMIT, 1.5, 3200)


def test_stale_market_refresh_reuses_snapshot_and_remains_stale(executor: tuple[TQExecutor, FakeApi]) -> None:
    from axile.executor.algorithms.utils.order_tracker import OrderTracker

    instance, api = executor
    first = instance.get_market_data(["rb2610"])["rb2610"]
    # 算法侧使用 symbol 级代理；刷新仍走真实 TQ 取价路径。
    proxy = SimpleNamespace(
        symbol="rb2610",
        logger=instance.logger,
        get_market_data=lambda: instance.get_market_data(["rb2610"])["rb2610"],
    )
    tracker = OrderTracker(executor=proxy)
    tracker.latest_prices["rb2610"] = first
    assert tracker._usable_price("rb2610") is None
    again = instance.get_market_data(["rb2610"])["rb2610"]
    assert again.timestamp == first.timestamp == 1_700_000_000_000
    assert instance.get_tick_size("rb2610") == 1
    assert api.quote_calls == 1


def test_missing_first_quote_keeps_code_directory_but_cannot_supply_specs(
    executor: tuple[TQExecutor, FakeApi], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, api = executor

    def pending_quote(_symbol: str) -> asyncio.Future:
        return asyncio.get_running_loop().create_future()

    monkeypatch.setattr(api, "get_quote", pending_quote)
    runtime = instance._require_runtime()
    read = runtime.quote_snapshots
    monkeypatch.setattr(runtime, "quote_snapshots", lambda symbols, **_kwargs: read(symbols, timeout=0.02))
    instance._ensure_instrument_symbols(["rb2610"])
    assert instance._catalog_record("rb2610").multiplier is None
    assert instance.get_market_data(["rb2610"]) == {}
    with pytest.raises(TimeoutError, match="首次行情订阅超时"):
        instance.get_tick_size("rb2610")
    assert not api.insert_args


def test_tq_uses_quote_specs_even_when_shared_catalog_contains_ctp_specs(executor):
    from axile.executor.instrument_catalog_records import Instrument

    instance, api = executor
    provider = instance._instrument_provider
    provider.catalog.register(
        "ctp", lambda symbols, timeout: [Instrument("SHFE", "rb2610", "FUTURE", 100, 5)], channel="ctp"
    )
    api.quote["price_tick"] = 2
    api.quote["volume_multiple"] = 20
    assert instance.get_tick_size("rb2610") == 2
    assets = UnifiedAccountAssets(available_cash=1_000_000, total_asset=1_000_000, market_value=0, positions=[])
    assert instance._calculate_generic_volume(0.32, 3200, assets, {}, symbol="rb2610") == 5
    assert instance.get_account_assets().positions[0].market_value == 3200 * 3 * 20
