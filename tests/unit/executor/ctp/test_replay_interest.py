"""CTP 既有缓存只用于相关品种选择，不补造账户事实。"""

from types import SimpleNamespace

from axile.executor.account_orders import AccountOrderBook
from axile.executor.ctp.ctp_execute import CTPExecutor


def test_existing_position_query_only_supplies_interest_without_new_query():
    executor = object.__new__(CTPExecutor)
    executor.account_orders = AccountOrderBook()
    executor.connection_records = {"query_positions": [SimpleNamespace(InstrumentID="rb2701", Position=2)]}

    def forbidden(*args):
        raise AssertionError("scope-only interest must not query the counterparty")

    executor._query = forbidden
    assert executor.replay_interest_symbols() == {"orders": [], "positions": ["rb2701"]}
