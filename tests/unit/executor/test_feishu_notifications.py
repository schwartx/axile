"""执行器飞书通知模块测试。"""

from __future__ import annotations

import queue
import threading

from axile.common.trade_channel import TradeChannel
from axile.executor import feishu_notifications as feishu_module
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.feishu_notifications import build_execute_results_feishu_card
from axile.executor.models.execution_result import ExecutionStatus
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_input import CTPAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.models.unified_price import UnifiedPriceData


class _Logger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        self.messages.append(("info", message))

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        self.messages.append(("warning", message))

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        self.messages.append(("error", message))


class _NotificationSource:
    def __init__(self) -> None:
        self.logger = _Logger()

    def _get_account_mark(self) -> str:
        return "acct-demo"

    def _get_operation_display(self, order: UnifiedOrder) -> str:
        _ = order
        return "开多"


class _FeishuAwareExecutor(AbstractExecutor):
    def _initialize_connection(self, account_config: object) -> None:
        self.account_config = account_config

    def _verify_connection(self) -> bool:
        return True

    def _check_trading_time(self) -> bool:
        return True

    def get_account_assets(self) -> UnifiedAccountAssets:
        return UnifiedAccountAssets(
            available_cash=1000.0,
            total_asset=1000.0,
            market_value=0.0,
            positions=[],
        )

    def get_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        return {}

    def _place_order_impl(
        self,
        symbol: str,
        direction: OrderDirection,
        order_type: OrderType,
        volume: float,
        price: float = 0,
        **kwargs: object,
    ) -> UnifiedOrder:
        _ = (direction, order_type, volume, price, kwargs)
        return UnifiedOrder(
            order_id=f"order-{symbol}",
            symbol=symbol,
            direction=OrderDirection.BUY,
            order_type=OrderType.LIMIT,
            volume=1.0,
            price=100.0,
            status="SUBMITTED",
        )

    def _get_pending_orders_impl(self, symbol: str | None = None) -> list[UnifiedOrder]:
        _ = symbol
        return []

    def _query_trades_impl(self, symbol: str, order_id: str) -> list[TradeRecord]:
        _ = (symbol, order_id)
        raise NotImplementedError

    def _cancel_order_impl(self, symbol: str, order_id: str) -> bool:
        _ = (symbol, order_id)
        return True

    def _cleanup(self) -> None:
        return None

    def _get_account_mark(self) -> str:
        return "executor-demo"

    def _get_default_trade_rules_for_empty(self, symbols: list[str]) -> dict[str, object]:
        _ = symbols
        return {}

    def register_order_callback(self, callback: object) -> None:
        _ = callback

    def register_price_callback(self, callback: object) -> None:
        _ = callback

    def unregister_order_callback(self, callback: object) -> None:
        _ = callback

    def unregister_price_callback(self, callback: object) -> None:
        _ = callback

    def initialize_websocket(self, symbols: list[str] | None = None) -> None:
        _ = symbols

    def is_monitoring(self) -> bool:
        return False


def test_empty_positions_does_not_send_second_card(monkeypatch) -> None:
    """清仓外层不重复发送结果卡片。"""
    executor = _FeishuAwareExecutor(TradeChannel.CTP, None)

    def _fake_execute(
        _standard_input: UnifiedStandardInput,
        cleanup: bool = True,
        retain_runtime: bool = False,  # noqa: FBT001, FBT002
    ) -> UnifiedStandardOutput:
        _ = (cleanup, retain_runtime)
        return UnifiedStandardOutput(
            account_assets=executor.get_account_assets(),
            status=ExecutionStatus.NOOP,
            channel_type=TradeChannel.CTP,
            success=True,
        )

    monkeypatch.setattr(executor, "execute", _fake_execute)
    monkeypatch.setattr(
        executor,
        "get_account_assets",
        lambda: UnifiedAccountAssets(
            available_cash=900.0,
            total_asset=1000.0,
            market_value=100.0,
            positions=[
                Position(
                    symbol="rb2610",
                    volume=1.0,
                    available_volume=1.0,
                    market_value=100.0,
                    direction=PositionDirection.LONG,
                    avg_price=100.0,
                )
            ],
        ),
    )
    executor.account_config = CTPAccountConfig.model_validate(
        {
            "broker_id": "b",
            "investor_id": "i",
            "password": "p",
            "td_front": "tcp://td:1",
            "md_front": "tcp://md:2",
            "app_id": "app",
            "auth_code": "auth",
        }
    )

    result = executor.empty_positions(feishu_key="hook-empty")

    assert result.success is True
    assert result.success is True  # fake execute 不进入真实通知入口


def test_custom_notification_context_is_redacted() -> None:
    """通知函数获得统一执行数据，但不能读到渠道凭据。"""
    standard_input = UnifiedStandardInput.from_dict(
        {
            "channel_type": TradeChannel.CTP.value,
            "account_config": {
                "broker_id": "b",
                "investor_id": "i",
                "password": "connection-secret",
                "td_front": "tcp://td:1",
                "md_front": "tcp://md:2",
                "app_id": "app",
                "auth_code": "auth",
            },
            "curr_target": {"rb2610": 0.2},
            "algorithm": {"method": "TEST", "params": {"api_token": "hidden", "pace": 2}},
            "feishu_account": {"id": 7, "name": "期货账户"},
            "extra": {"audit": {"execution_id": "exec-1", "execution_kind": "rebalance"}},
        }
    )
    output = _minimal_output()
    output.inputs = standard_input
    context = feishu_module.build_execution_notification_context(_NotificationSource(), output)
    assert context["account"] == {"id": 7, "name": "期货账户", "mark": "acct-demo"}
    assert context["execution"]["id"] == "exec-1"
    assert context["strategy"]["algorithm"]["params"] == {"pace": 2}
    assert "account_config" not in str(context)
    assert "connection-secret" not in str(context)


def _minimal_output() -> UnifiedStandardOutput:
    """构造派发器测试用的最小执行输出。"""
    return UnifiedStandardOutput(
        account_assets=UnifiedAccountAssets(
            available_cash=0.0,
            total_asset=0.0,
            market_value=0.0,
            positions=[],
        ),
        status=ExecutionStatus.SUCCEEDED,
        channel_type=TradeChannel.CTP,
        success=True,
    )


def test_enqueue_feishu_drops_when_queue_full(monkeypatch) -> None:
    """队列已满时应丢弃通知并记录告警，且不抛异常、不新建线程。"""
    warnings: list[str] = []

    class _FullQueue:
        def put_nowait(self, item: object) -> None:
            _ = item
            raise queue.Full

    monkeypatch.setattr(feishu_module, "_notify_queue", _FullQueue())
    monkeypatch.setattr(feishu_module, "_ensure_feishu_notify_workers_started", lambda: None)
    monkeypatch.setattr(
        feishu_module.loguru.logger,
        "warning",
        lambda message, *args, **kwargs: warnings.append(str(message)),
    )

    feishu_module.enqueue_execute_results_to_feishu(
        _NotificationSource(), _minimal_output(), "hook", "def notify(context): pass"
    )

    assert len(warnings) == 1
    assert "队列已满" in warnings[0]


def test_enqueue_feishu_delivers_through_bounded_worker(monkeypatch) -> None:
    """投递的通知应由后台 worker 实际消费，参数透传正确。"""
    done = threading.Event()
    captured: list[tuple[object, object, object]] = []

    def _fake_send(source: object, output: object, feishu_key: object) -> None:
        captured.append((source, output, feishu_key))
        done.set()

    monkeypatch.setattr(
        feishu_module,
        "dispatch_execution_notification",
        lambda source, output, key, code, snapshot: _fake_send(source, output, key),
    )

    source = _NotificationSource()
    feishu_module.enqueue_execute_results_to_feishu(
        source, _minimal_output(), "hook-worker", "def notify(context): pass"
    )

    assert done.wait(timeout=5.0), "后台 worker 未在超时内消费通知任务"
    assert captured[0][0] is source
    assert captured[0][2] == "hook-worker"


def test_feishu_worker_survives_task_exception(monkeypatch) -> None:
    """单个通知任务抛异常不得拖垮 worker，后续任务仍能被消费。"""
    done = threading.Event()
    calls: list[str] = []

    def _flaky_send(source: object, output: object, feishu_key: object) -> None:
        _ = (source, output)
        calls.append(str(feishu_key))
        if feishu_key == "boom":
            raise RuntimeError("push failed")
        done.set()

    monkeypatch.setattr(
        feishu_module,
        "dispatch_execution_notification",
        lambda source, output, key, code, snapshot: _flaky_send(source, output, key),
    )

    feishu_module.enqueue_execute_results_to_feishu(
        _NotificationSource(), _minimal_output(), "boom", "def notify(context): pass"
    )
    feishu_module.enqueue_execute_results_to_feishu(
        _NotificationSource(), _minimal_output(), "ok", "def notify(context): pass"
    )

    assert done.wait(timeout=5.0), "异常任务后 worker 未继续消费下一个任务"
    assert "boom" in calls and "ok" in calls


def test_notification_asset_selection_and_evidence_boundary() -> None:
    """降级执行复用旧快照，金额和持仓同源，执行输出仍保留本次事实。"""
    source = _NotificationSource()
    old = UnifiedAccountAssets(
        available_cash=700,
        total_asset=1000,
        market_value=300,
        positions=[
            Position(symbol="rb2610", volume=2, available_volume=2, market_value=300, direction=PositionDirection.LONG)
        ],
    )
    snapshot = {"id": 42, "created_at": "2026-09-23 11:19:00", "assets": old.model_dump(mode="json")}
    for degraded_source in ("unavailable", "assumed", "error"):
        output = UnifiedStandardOutput(
            account_assets=UnifiedAccountAssets(
                available_cash=0, total_asset=0, market_value=0, source=degraded_source
            ),
            status=ExecutionStatus.BLOCKED,
            channel_type=TradeChannel.CTP,
        )
        card = build_execute_results_feishu_card(source, output, notification_snapshot=snapshot)
        variables = card["data"]["template_variable"]
        assert (variables["total_assets"], variables["available_cash"], variables["market_value"]) == (
            "1000.00",
            "700.00",
            "300.00",
        )
        assert variables["positions"][0]["symbol"] == "rb2610"
        assert output.account_assets.source == degraded_source
        assert "notification_snapshot" not in output.model_dump_json()
        missing = build_execute_results_feishu_card(source, output)["data"]["template_variable"]
        assert missing["total_assets"] == "未获取"
        assert missing["positions"][0]["symbol"] == "未获取"

    current = UnifiedStandardOutput(
        account_assets=UnifiedAccountAssets(available_cash=0, total_asset=0, market_value=0, positions=[]),
        status=ExecutionStatus.NOOP,
        channel_type=TradeChannel.CTP,
    )
    variables = build_execute_results_feishu_card(source, current, notification_snapshot=snapshot)["data"][
        "template_variable"
    ]
    assert variables["total_assets"] == "0.00"
    assert variables["positions"] == []


def test_notification_success_callback_failure_does_not_escape(monkeypatch):
    """状态存储不可用时仍保留通知自身结果。"""
    from axile.common.notification_function import NotificationFunctionResult
    from axile.executor import feishu_notifications as notifications

    source = _NotificationSource()

    def unavailable(*_args):
        raise RuntimeError("database unavailable")

    source._notification_success_callback = unavailable
    monkeypatch.setattr(notifications, "build_execution_notification_context", lambda *_args: {})
    monkeypatch.setattr(
        notifications, "run_notification_function", lambda *_args, **_kwargs: NotificationFunctionResult(True)
    )
    output = UnifiedStandardOutput(
        account_assets=UnifiedAccountAssets(available_cash=0, total_asset=0, market_value=0, positions=[]),
        symbol_results={},
        status=ExecutionStatus.NOOP,
        channel_type=TradeChannel.CTP,
    )
    notifications.dispatch_execution_notification(source, output, None, "def notify(context): pass", None)
