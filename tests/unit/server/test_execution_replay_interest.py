"""缓存只扩大行情采集兴趣，不被包装成已确认订单或持仓。"""

from axile.executor.abstract_executor.facade import AbstractExecutorFacadeMixin
from axile.executor.account_orders import AccountOrderBook
from axile.executor.models.unified_order import UnifiedOrder
from axile.server.execution_replay.recorder import ReplayRecorder
from tests.unit.server.test_execution_replay_recorder import FakeWriter, eventually


class Owner(AbstractExecutorFacadeMixin):
    def __init__(self):
        self.account_orders = AccountOrderBook()
        self.interest_reads = 0

    def replay_interest_symbols(self):
        self.interest_reads += 1
        return super().replay_interest_symbols()


def pending(symbol):
    return UnifiedOrder(
        order_id="known-before-recording",
        symbol=symbol,
        direction="BUY",
        order_type="LIMIT",
        volume=3,
        price=3500,
        status="未成交",
    )


def test_preexisting_order_only_retains_future_quotes_without_fabricated_facts(tmp_path):
    owner = Owner()
    owner.account_orders.apply(pending("A"))
    writer = FakeWriter()
    recorder = ReplayRecorder(7, tmp_path, writer_factory=lambda *args: writer, now_us=lambda: 1_000_000)
    recorder.attach(owner)
    assert owner.interest_reads == 0
    recorder.configure(True, 1)
    eventually(lambda: owner.interest_reads == 1)
    recorder.observe("/quote/A", {"symbol": "A", "bids": [{"price": 3500, "quantity": 2}], "asks": []})
    recorder.close()
    assert [message["topic"] for message in writer.messages] == ["/quote/A"]
    assert owner.account_orders.known_symbols() == ["A"]
