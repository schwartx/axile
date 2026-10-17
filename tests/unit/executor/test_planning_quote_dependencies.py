"""零目标无仓的定手数合约不应阻塞其他合约的行情准备。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axile.executor.abstract_executor.capability import AbstractExecutorCapabilityMixin
from axile.executor.execution_engine import ExecutionEngine
from axile.executor.models.unified_account_assets import Position, UnifiedAccountAssets


class Owner(AbstractExecutorCapabilityMixin):
    logger = MagicMock()

    def __init__(self):
        self.requests = []

    def get_market_data(self, symbols):
        self.requests.append(symbols)
        if "old" in symbols:
            raise TimeoutError("old quote")
        return {symbol: SimpleNamespace(last_price=100) for symbol in symbols}

    def get_min_notional(self, symbol):
        return None


@pytest.mark.parametrize(
    "source,held,mode,blocked",
    [
        ("real", False, "lots", False),
        ("real", True, "lots", True),
        ("unavailable", False, "lots", True),
        ("real", False, "weight", True),
    ],
)
def test_only_trustworthy_zero_lots_without_gross_positions_skip_quote(source, held, mode, blocked):
    owner = Owner()
    engine = ExecutionEngine(owner, runtime=object())
    positions = (
        [Position(symbol="old", direction="多头", volume=1, available_volume=1, price=100, market_value=100)]
        if held
        else []
    )
    assets = UnifiedAccountAssets(
        available_cash=1000, total_asset=1000, market_value=100 if held else 0, positions=positions, source=source
    )
    inputs = SimpleNamespace(
        curr_target={"active": 1},
        last_target={"old": 1},
        trade_rules={"old": {"sizing_mode": mode}},
        forbidden_symbols=None,
    )

    def plan():
        return engine._plan_target_volumes_for_symbols(
            standard_input=inputs, account_assets=assets, symbols=["active", "old"], effective_curr_target={"active": 1}
        )

    if blocked:
        with pytest.raises(TimeoutError):
            plan()
    else:
        quotes, sizing = plan()
        assert owner.requests == [["active"]]
        assert set(quotes) == {"active"}
        assert sizing["old"].target_quantity == 0
