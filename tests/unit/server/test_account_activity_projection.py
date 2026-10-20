"""活动流列表只发布摘要，不把执行原料 JSON 带给前端。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from axile.server.api.routes.account_schedule import account_activity_symbols

_FAT_RAW = {
    "status": "SUCCEEDED",
    "execution_kind": "rebalance",
    "execution_time": 1.5,
    "memory": {"scratch": True},
    "inputs": {"ignored": 1},
    "extra": {"channel": "tq"},
    "account_assets": {
        "total_asset": 10000.0,
        "available_cash": 1.0,
        "positions": [{"symbol": "rb2610", "volume": 1, "direction": "long"}],
    },
    "symbol_results": {
        "rb2610": {
            "status": "SUCCEEDED",
            "error": None,
            "outcome": "success",
            "sizing": {"unit_multiplier": 10, "current_quantity": 0, "target_quantity": 1},
            "first_tick": {"bid_price": 99, "ask_price": 101, "last_price": 100},
            "orders": [{"order_id": "o1", "direction": "BUY", "filled_volume": 1}],
            "trades": [
                {
                    "trade_price": 100,
                    "trade_volume": 1,
                    "order_id": "o1",
                    "trade_time": "2026-09-01T09:00:01",
                    "extra": {},
                }
            ],
            "memory": {"tick": 1},
        }
    },
}


def _record(**overrides) -> SimpleNamespace:
    payload = {
        "id": 7,
        "execution_id": "exec-7",
        "account_id": 1,
        "raw_input": {"curr_target": {"rb2610": 1}, "algorithm": {"method": "TWAP"}},
        "raw_result": dict(_FAT_RAW),
        "is_success": 1,
        "created_at": "2026-09-01T09:00:00",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


class _Result:
    def __init__(self, rows=None, count=None):
        self._rows = rows
        self._count = count
        self.closed = False

    def scalar_one(self):
        return self._count

    def scalars(self):
        return self

    def all(self):
        return self._rows

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for row in self._rows:
            yield row


class _FakeSession:
    def __init__(self, plan):
        self._plan = list(plan)

    async def execute(self, _stmt):
        return self._plan.pop(0)

    async def stream_scalars(self, _stmt):
        return self._plan.pop(0)

    async def scalar(self, _stmt):
        return self._plan.pop(0).scalar_one()

    async def get(self, _model, account_id):
        return SimpleNamespace(id=account_id)


def _call(fn, **kwargs):
    return asyncio.run(fn(**kwargs))


def test_activity_symbols_include_fills_when_one_symbol_is_requested():
    """展开某个品种时，同一接口带上该品种成交。"""
    result = _Result(rows=[_record()])
    session = _FakeSession([result])
    payload = _call(
        account_activity_symbols,
        session=session,
        account_id=1,
        since="2026-09-01T00:00:00",
        until="2026-09-02T00:00:00",
        symbol="rb2610",
    )
    assert result.closed
    assert payload.count == 1
    row = payload.data[0]
    assert row.symbol == "rb2610"
    assert len(row.trades) == 1
    assert row.trades[0]["value"] == 1000.0
