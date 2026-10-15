"""账户会话复用、故障冷却与读请求隔离，不依赖真实交易服务。"""

from threading import Event, Thread

import pytest

from axile.common.trade_channel import TradeChannel
from axile.server.execution import resident_connection as resident
from tests.unit.server._execution_test_support import build_account


class Executor:
    def __init__(self):
        self.ready = False
        self.calls = []

    def _verify_connection(self):
        self.calls.append("check")
        return self.ready

    def get_account_assets(self):
        self.calls.append("assets")
        return object()

    def get_pending_orders(self, symbol):
        assert symbol is None
        self.calls.append("orders")
        return []


def account(**kwargs):
    return build_account(id=7, trade_channel=TradeChannel.GM, **kwargs)


def harness(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(resident, "clock_monotonic", lambda: now[0])
    state = resident.ResidentConnectionState()
    created, closed, observations = [], [], []
    state.connection_observer = observations.append

    def create():
        executor = Executor()
        created.append(executor)
        return executor

    def initialize(executor):
        executor.calls.append("initialize")
        executor.ready = True

    def ensure(**kwargs):
        return resident.ensure_resident_connection(
            state, account(), create=create, close=closed.append, initialize=initialize, **kwargs
        )

    return now, state, created, closed, observations, ensure


def test_reads_reuse_one_session_and_reconcile_only_at_connection_boundary(monkeypatch):
    _, _, created, closed, observed, ensure = harness(monkeypatch)
    executor = ensure()
    assert executor.calls[-2:] == ["assets", "orders"]
    for _ in range(3):
        assert ensure() is executor
    assert len(created) == 1
    assert closed == []
    assert executor.calls.count("initialize") == 1
    assert executor.calls.count("orders") == 1
    assert observed[-1].status == "ok"


def test_native_recovery_reconciles_without_restarting_sdk(monkeypatch):
    _, _, created, closed, _, ensure = harness(monkeypatch)
    executor = ensure()
    executor.connection_revision = 1
    assert ensure() is executor
    assert executor.calls.count("initialize") == 1
    assert executor.calls.count("orders") == 2
    assert ensure() is executor
    assert executor.calls.count("orders") == 2
    assert len(created) == 1 and closed == []


def test_disconnected_reads_never_relogin_even_after_cooldown(monkeypatch):
    now, state, created, closed, _, ensure = harness(monkeypatch)
    executor = ensure()
    executor.ready = False
    for timestamp in [1, 2, 31, 300]:
        now[0] = timestamp
        with pytest.raises(resident.AccountConnectionUnavailable):
            ensure()
    assert state.connection_error is not None
    assert len(created) == 1
    assert closed == []
    assert executor.calls.count("initialize") == 1


def test_execution_prepare_obeys_cooldown_and_reconciles_replacement(monkeypatch):
    now, _, created, closed, _, ensure = harness(monkeypatch)
    old = ensure()
    old.ready = False
    with pytest.raises(resident.AccountConnectionUnavailable):
        ensure(allow_recovery=True)
    now[0] = 29
    with pytest.raises(resident.AccountConnectionUnavailable):
        ensure(allow_recovery=True)
    assert len(created) == 1
    now[0] = 30
    new = ensure(allow_recovery=True)
    assert new is not old
    assert closed == [old]
    assert new.calls[-2:] == ["assets", "orders"]


def test_explicit_reconnect_bypasses_cooldown_but_keeps_healthy_session(monkeypatch):
    _, _, created, _, _, ensure = harness(monkeypatch)
    old = ensure()
    assert ensure(allow_recovery=True, force=True) is old
    old.ready = False
    with pytest.raises(resident.AccountConnectionUnavailable):
        ensure()
    new = ensure(allow_recovery=True, force=True)
    assert new is not old
    assert len(created) == 2


def test_constructor_failure_is_cached_even_without_an_executor(monkeypatch):
    now, state, _, _, observed, _ = harness(monkeypatch)
    attempts = []
    original = TimeoutError("GetEncryptedToken deadline")

    def create():
        attempts.append(True)
        raise original

    def ensure(**kwargs):
        return resident.ensure_resident_connection(
            state, account(), create=create, close=lambda _: None, initialize=lambda _: None, **kwargs
        )

    with pytest.raises(TimeoutError) as caught:
        ensure()
    assert caught.value is original
    for _ in range(3):
        with pytest.raises(resident.AccountConnectionUnavailable) as caught:
            ensure(allow_recovery=True)
        assert caught.value.retry_after == 30
        assert caught.value.technical_detail == str(original)
    assert len(attempts) == 1
    assert state.executor is None
    assert observed[-1].status == "cooldown"
    now[0] = 30
    with pytest.raises(TimeoutError):
        ensure(allow_recovery=True)
    assert len(attempts) == 2


def test_manual_disconnect_stays_disconnected_until_explicit_reconnect(monkeypatch):
    _, state, created, closed, observed, ensure = harness(monkeypatch)
    old = ensure()
    resident.disconnect_resident_executor(state, close=closed.append)
    for kwargs in [{}, {"allow_recovery": True}]:
        with pytest.raises(resident.AccountConnectionUnavailable, match="主动断开"):
            ensure(**kwargs)
    assert created == [old]
    assert closed == [old]
    assert observed[-1].status == "disconnected"
    assert ensure(allow_recovery=True, force=True) is not old
    assert state.disconnected is False


def test_concurrent_cold_reads_share_one_initialization(monkeypatch):
    _, state, _, _, _, _ = harness(monkeypatch)
    entered, release = Event(), Event()
    executor = Executor()
    created, results, errors = [], [], []

    def create():
        created.append(executor)
        return executor

    def initialize(instance):
        entered.set()
        assert release.wait(2)
        instance.ready = True

    def run():
        try:
            results.append(
                resident.ensure_resident_connection(
                    state, account(), create=create, close=lambda _: None, initialize=initialize
                )
            )
        except BaseException as exc:
            errors.append(exc)

    first, second = Thread(target=run), Thread(target=run)
    first.start()
    assert entered.wait(1)
    second.start()
    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert results == [executor, executor]
    assert created == [executor]


def test_reconciliation_failure_does_not_advertise_ready(monkeypatch):
    _, state, _, _, observed, ensure = harness(monkeypatch)

    def reconcile(_):
        raise TimeoutError("pending orders unknown")

    with pytest.raises(TimeoutError):
        ensure(reconcile=reconcile)
    assert state.connected_once is False
    assert observed[-1].status == "cooldown"
    # SDK 线程已存活也不能绕过对账失败，下一次读取不能当作连接恢复成功。
    with pytest.raises(resident.AccountConnectionUnavailable):
        ensure(reconcile=reconcile)
