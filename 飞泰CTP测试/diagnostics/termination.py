"""#108 核心终止清理表征：原生 SDK 用 Mock，不创建连接或发送真实委托。"""

import json
from datetime import datetime
from threading import Event
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from axile.common.trade_channel import TradeChannel
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.presets import resolve_account_control_policy
from axile.executor.account_control.snapshot import AccountControlCounterSnapshot
from axile.executor.ctp.ctp_execute import CTPExecutor
from axile.executor.execution_session import ExecutionSession
from axile.executor.models.unified_input import CTPAccountConfig
from axile.executor.termination import ExecutionTerminated, ExecutionTerminationController


def executor():
    owner = CTPExecutor(TradeChannel.CTP)
    owner.account_config = CTPAccountConfig(
        broker_id="test",
        investor_id="test",
        password="test",
        td_front="tcp://unused:1",
        md_front="tcp://unused:2",
        app_id="test",
        auth_code="test",
    )
    owner._ready = owner._trader_connected = owner._market_connected = True
    owner._trading_day = "20261016"
    owner._trader_api = Mock()
    owner._trader_api.ReqQryOrder.return_value = 0
    owner._trader_api.ReqOrderAction.return_value = 0
    owner._order_keys["known"] = {
        "order_ref": "1",
        "front_id": 1,
        "session_id": 1,
        "exchange_id": "CFFEX",
        "order_sys_id": "1",
    }
    stop = Event()
    owner.set_termination_controller(
        ExecutionTerminationController(
            cancel_event=stop, reason_provider=lambda: "diagnostic stop", mode_provider=lambda: "cancel_pending"
        )
    )
    owner.prepare_execution_runtime()
    return owner, stop


def main():
    owner, stop = executor()
    stop.set()
    try:
        ExecutionSession(owner=owner, symbol="T2612").handle_termination_checkpoint()
        raise AssertionError("termination not observed")
    except ExecutionTerminated as error:
        print(
            json.dumps(
                {
                    "probe": "CTP cancel_pending query checkpoint",
                    "query_native_calls": owner._trader_api.ReqQryOrder.call_count,
                    "cancel_native_calls": owner._trader_api.ReqOrderAction.call_count,
                    "cancel_failed_order_ids": error.cancel_failed_order_ids,
                    "observed": "query aborts before native API; empty failed list does not mean no pending orders",
                },
                ensure_ascii=False,
            )
        )
        assert owner._trader_api.ReqQryOrder.call_count == owner._trader_api.ReqOrderAction.call_count == 0

    owner, stop = executor()
    policy = resolve_account_control_policy("ctp")
    base = datetime(2026, 10, 16, 13, 25, 39, tzinfo=ZoneInfo("Asia/Shanghai"))
    guard = AccountControlGuard(
        account_id=1,
        execution_id="diagnostic",
        channel=TradeChannel.CTP,
        policy=policy,
        baseline=AccountControlCounterSnapshot(),
        clock=lambda: base,
    )
    owner.set_account_control_guard(guard)
    guard.begin_operation("place_order", symbol="T2612").record_outcome("broker_acknowledged")
    stop.set()
    try:
        ExecutionSession(owner=owner, symbol="T2612").cancel_order("known")
        raise AssertionError("cancel unexpectedly sent")
    except ExecutionTerminated as error:
        print(
            json.dumps(
                {
                    "probe": "risk cancellation under CTP shared throttle",
                    "cancel_native_calls": owner._trader_api.ReqOrderAction.call_count,
                    "exception": type(error).__name__,
                    "observed": "cancel_order_ctp quota wait is interrupted by the same termination",
                },
                ensure_ascii=False,
            )
        )
        assert owner._trader_api.ReqOrderAction.call_count == 0


if __name__ == "__main__":
    main()
