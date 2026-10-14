"""worker backend 入口的单元测试。"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import cast

import pytest

from axile.common.gm_symbols import normalize_gm_standard_input
from axile.common.trade_channel import TradeChannel
from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.executor.models.execution_result import AlgorithmResult
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import ExecutionStatus, UnifiedStandardOutput
from axile.server.db.models import Account
from axile.server.execution.worker_backend import worker as worker_backend_entry
from axile.server.execution.worker_backend import worker_responses, worker_state
from axile.server.execution.worker_backend.protocol import WorkerBackendRequest, WorkerBackendResponse
from tests.unit.server._execution_test_support import build_account


class _FakeWorkerExecutor:
    def __init__(self) -> None:
        self.execute_input: UnifiedStandardInput | None = None
        self.empty_kwargs: dict[str, object] = {}
        self.audit_seq = 0

    def next_audit_seq(self) -> int:
        self.audit_seq += 1
        return self.audit_seq

    def _normalize_standard_input(self, standard_input: UnifiedStandardInput) -> UnifiedStandardInput:
        if standard_input.channel_type == TradeChannel.GM:
            return normalize_gm_standard_input(standard_input)
        return standard_input

    @staticmethod
    def _normalize_connected_standard_input(standard_input: UnifiedStandardInput) -> UnifiedStandardInput:
        return standard_input

    def execute(
        self,
        standard_input: UnifiedStandardInput,
        *,
        cleanup: bool = True,  # noqa: FBT001, FBT002
        retain_runtime: bool = False,  # noqa: FBT001, FBT002
    ) -> UnifiedStandardOutput:
        _ = cleanup, retain_runtime
        self.execute_input = standard_input
        return UnifiedStandardOutput(
            account_assets=UnifiedAccountAssets(
                available_cash=1000.0,
                total_asset=1000.0,
                market_value=0.0,
                positions=[],
            ),
            memory={"ts": datetime(2026, 3, 27, 10, 0, 0)},
            inputs=standard_input,
            execution_time=0.1,
            channel_type=TradeChannel.GM,
            symbol_results={
                "SHSE.600000": AlgorithmResult(
                    symbol="SHSE.600000",
                    algorithm="SINGLE-MAKER",
                    status=ExecutionStatus.SUCCEEDED,
                    orders=[],
                    target_volume=100,
                    first_tick=None,
                    memory={"status": "done"},
                )
            },
            status=ExecutionStatus.SUCCEEDED,
            success=True,
        )

    def empty_positions(
        self,
        *,
        cleanup: bool = True,  # noqa: FBT001, FBT002
        retain_runtime: bool = False,  # noqa: FBT001, FBT002
        **kwargs: object,
    ) -> UnifiedStandardOutput:
        _ = cleanup, retain_runtime
        self.empty_kwargs = kwargs
        return UnifiedStandardOutput(
            account_assets=UnifiedAccountAssets(
                available_cash=900.0,
                total_asset=900.0,
                market_value=0.0,
                positions=[],
            ),
            memory={"ts": datetime(2026, 3, 27, 10, 0, 0)},
            inputs=None,
            execution_time=0.2,
            channel_type=TradeChannel.GM,
            symbol_results={},
            status=ExecutionStatus.NOOP,
            success=True,
        )


def _build_gm_account() -> Account:
    return Account(
        id=2,
        name="gm-worker-test",
        market="A股",
        trade_channel=TradeChannel.GM,
        account_control_preset="default",
        account_control_override=None,
        account_config={
            "account_id": "gm-account",
            "token": "token",
            "connection_mode": "service",
            "serv_addr": "127.0.0.1:7001",
        },
        is_started=True,
        cron_expr="* * * * *",
        remark=None,
        brokerage="gm",
        weight_precision=0.01,
        long_leverage=1.0,
        short_leverage=0.0,
        algorithm={"method": "SINGLE-MAKER", "params": {}},
        empty_positions_algorithm=None,
        trade_rules={"SHSE.600000": {"min_notional": 100.0}},
        forbidden_symbols=[],
        risk_symbols=[],
        feishu_key=None,
        portfolio_id=1,
        write_empty_record=0,
    )


def test_resolve_executor_recreates_disconnected_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    account = build_account()
    stale = _FakeWorkerExecutor()
    stale._verify_connection = lambda: False  # type: ignore[attr-defined]
    replacement = _FakeWorkerExecutor()
    state = worker_backend_entry._WorkerBackendState(
        executor=stale,
        account_id=account.id,
        config_signature=worker_state._config_signature(account),
    )
    closed: list[object] = []

    monkeypatch.setattr(worker_state, "_close_executor", closed.append)
    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account: replacement)

    assert worker_state._resolve_executor(state, account) is replacement
    assert closed == [stale]


def test_resolve_executor_rejects_wrong_expected_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    account = build_account()
    executor = _FakeWorkerExecutor()
    executor._trading_day = "20260821"  # type: ignore[attr-defined]
    state = worker_backend_entry._WorkerBackendState()
    closed: list[object] = []

    monkeypatch.setattr(worker_state, "_close_executor", closed.append)
    monkeypatch.setattr(worker_state, "create_executor_instance", lambda _account: executor)

    with pytest.raises(RuntimeError, match="expected=20260824"):
        worker_state._resolve_executor(state, account, "20260824")

    assert closed == [executor]
    assert state.executor is None
    assert state.account_id is None


def test_finalize_executor_flushes_before_clearing_request_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    class _RequestBoundExecutor:
        def set_account_control_guard(self, value: object) -> None:
            calls.append(("guard", value))

        def set_termination_controller(self, value: object) -> None:
            calls.append(("termination", value))

        def set_audit_sink(self, value: object) -> None:
            calls.append(("audit_sink", value))

        def set_audit_context(self, value: object) -> None:
            calls.append(("audit_context", value))

        def clear_execution_runtime(self) -> None:
            calls.append("runtime")

    async def fake_flush(_executor: object) -> None:
        calls.append("flush")

    monkeypatch.setattr(worker_state, "flush_account_control_records", fake_flush)

    worker_state._finalize_executor(_RequestBoundExecutor())

    assert calls == [
        "flush",
        ("guard", None),
        ("termination", None),
        ("audit_sink", None),
        ("audit_context", {}),
        "runtime",
    ]


def test_handle_prepare_returns_cached_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    account = build_account()
    executor = _FakeWorkerExecutor()
    executor._trading_day = "20260824"  # type: ignore[attr-defined]
    monkeypatch.setattr(worker_backend_entry, "_resolve_prepared_executor", lambda **_kwargs: executor)
    monkeypatch.setattr(worker_backend_entry, "_finalize_executor", lambda _executor: None)
    request = WorkerBackendRequest(
        request_id="req-prepare",
        command="prepare",
        account_payload=account.model_dump(mode="json"),
        execution_id=None,
        payload={"expected_trading_day": "20260824"},
    )

    response = worker_backend_entry._handle_prepare(request, worker_backend_entry._WorkerBackendState())

    assert response.kind == "result"
    assert response.output_payload == {"ready": True, "trading_day": "20260824"}


def test_handle_empty_positions_returns_result_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _build_gm_account()
    fake_executor = _FakeWorkerExecutor()
    captured: dict[str, object] = {}

    monkeypatch.setattr(worker_backend_entry, "_resolve_prepared_executor", lambda **_kwargs: fake_executor)
    monkeypatch.setattr(worker_backend_entry, "_finalize_executor", lambda _executor: None)

    def fake_append_empty_positions_pre_execute_audit(**kwargs: object) -> None:
        captured["pre_execute"] = kwargs

    def fake_append_success_audit(**kwargs: object) -> None:
        captured["success"] = kwargs

    monkeypatch.setattr(
        worker_backend_entry,
        "_append_empty_positions_pre_execute_audit",
        fake_append_empty_positions_pre_execute_audit,
    )
    monkeypatch.setattr(worker_backend_entry, "_append_success_audit", fake_append_success_audit)

    request = WorkerBackendRequest(
        request_id="req-2",
        command="empty_positions",
        account_payload=account.model_dump(mode="json"),
        execution_id="exec-worker-empty-1",
        payload={
            "empty_kwargs": {
                "algorithm": {"method": "SINGLE-MAKER"},
                "feishu_key": None,
                "extra": {
                    "audit": {
                        "execution_id": "exec-worker-empty-1",
                        "account_id": account.id,
                        "channel": account.trade_channel.value,
                        "algorithm": "SINGLE-MAKER",
                        "trigger_source": "empty_positions",
                        "execution_kind": "clear_positions",
                    }
                },
            },
            "audit_input": {"algorithm": {"method": "SINGLE-MAKER"}},
        },
    )

    response = worker_backend_entry._handle_empty_positions(request, worker_backend_entry._WorkerBackendState())

    assert response.kind == "result"
    assert response.output_payload is not None
    assert fake_executor.empty_kwargs["algorithm"] == {"method": "SINGLE-MAKER"}
    assert cast(dict[str, object], captured["pre_execute"])["algorithm_name"] == "SINGLE-MAKER"
    assert cast(dict[str, object], captured["success"])["include_trade_rule_snapshots"] is False


def test_handle_execute_trade_uses_account_channel_for_standard_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker backend 应按账户渠道反序列化标准输入，而不是写死 GM。"""
    account = build_account()
    fake_executor = _FakeWorkerExecutor()

    monkeypatch.setattr(worker_backend_entry, "_resolve_prepared_executor", lambda **_kwargs: fake_executor)
    monkeypatch.setattr(worker_backend_entry, "_finalize_executor", lambda _executor: None)
    monkeypatch.setattr(worker_backend_entry, "_append_trade_pre_execute_audit", lambda **_kwargs: None)
    monkeypatch.setattr(worker_backend_entry, "_append_success_audit", lambda **_kwargs: None)

    request = WorkerBackendRequest(
        request_id="req-ctp",
        command="execute_trade",
        account_payload=account.model_dump(mode="json"),
        execution_id="exec-worker-ctp-1",
        payload={
            "standard_input": {
                "channel_type": TradeChannel.GM.value,
                "account_config": account.account_config,
                "curr_target": {"rb2610": 0.1},
                "last_target": {},
                "algorithm": {"method": "SINGLE-MAKER", "params": {}},
                "trade_rules": {},
                "extra": {"audit": {"execution_id": "exec-worker-ctp-1"}},
            },
            "audit_input": {"curr_target": {"rb2610": 0.1}},
            "trigger_source": "manual",
            "cleanup": True,
        },
    )

    response = worker_backend_entry._handle_execute_trade(
        request,
        worker_backend_entry._WorkerBackendState(),
    )

    assert response.kind == "result"
    assert fake_executor.execute_input is not None
    assert fake_executor.execute_input.channel_type == TradeChannel.CTP
    assert fake_executor.execute_input.account_config.channel_type == TradeChannel.CTP


def test_handle_worker_shutdown_command_returns_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shutdown 命令应返回确认响应，并清理缓存执行器。"""
    finalized: list[object | None] = []
    state = worker_backend_entry._WorkerBackendState(executor=object(), account_id=2, config_signature="sig")

    monkeypatch.setattr(worker_state, "_close_executor", lambda executor: finalized.append(executor))

    response = worker_backend_entry._handle_worker_request(
        WorkerBackendRequest.shutdown("req-shutdown", reason="test"),
        state,
    )

    assert response.kind == "result"
    assert response.output_payload == {"shutdown": True, "reason": "test"}
    assert len(finalized) == 1
    assert state.executor is None
    assert state.config_signature is None


def test_handle_execute_trade_exception_returns_structured_error_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute_trade 异常应返回结构化错误响应，并写 execution_failed 审计。"""
    account = _build_gm_account()
    captured: dict[str, object] = {}

    class _BoomExecutor(_FakeWorkerExecutor):
        def execute(
            self,
            standard_input: UnifiedStandardInput,
            *,
            cleanup: bool = True,  # noqa: FBT001, FBT002
            retain_runtime: bool = False,  # noqa: FBT001, FBT002
        ) -> UnifiedStandardOutput:
            _ = standard_input, cleanup, retain_runtime
            raise RuntimeError("boom")

    monkeypatch.setattr(worker_backend_entry, "_resolve_prepared_executor", lambda **_kwargs: _BoomExecutor())
    monkeypatch.setattr(worker_backend_entry, "_finalize_executor", lambda _executor: None)
    monkeypatch.setattr(worker_backend_entry, "_append_trade_pre_execute_audit", lambda **_kwargs: None)
    monkeypatch.setattr(worker_responses.logger, "exception", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker_backend_entry,
        "_append_failed_audit",
        lambda **kwargs: captured.update(kwargs),
    )

    standard_input = UnifiedStandardInput.from_dict(
        {
            "channel_type": TradeChannel.GM.value,
            "account_config": account.account_config,
            "curr_target": {"SHSE.600000": 0.12},
            "last_target": {},
            "algorithm": {"method": "SINGLE-MAKER", "params": {}},
            "trade_rules": account.trade_rules,
            "extra": {"audit": {"execution_id": "exec-worker-trade-error-1"}},
        }
    )
    request = WorkerBackendRequest(
        request_id="req-error",
        command="execute_trade",
        account_payload=account.model_dump(mode="json"),
        execution_id="exec-worker-trade-error-1",
        payload={
            "standard_input": standard_input.to_dict(),
            "audit_input": {"curr_target": {"SHSE.600000": 0.12}},
            "trigger_source": "manual",
            "cleanup": True,
        },
    )

    response = worker_backend_entry._handle_execute_trade(
        request,
        worker_backend_entry._WorkerBackendState(),
    )

    assert response.kind == "error"
    assert response.error is not None
    assert response.error.type == "runtime_error"
    # message 面向用户展示：未知异常用固定人话，原文进 worker 日志。
    assert response.error.message == "worker 执行失败，具体原因未确认"
    assert response.error.retryable is False
    error_payload = cast(object, captured["error"])
    assert getattr(error_payload, "type") == "runtime_error"
    assert getattr(error_payload, "message") == "worker 执行失败，具体原因未确认"


def test_handle_execute_trade_marks_persistent_ctp_queue_limit_for_session_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = build_account(id=2)

    class _SessionRecoveryExecutor(_FakeWorkerExecutor):
        def get_account_assets(self) -> UnifiedAccountAssets:
            raise CtpSessionRecoveryRequired("ReqQryTradingAccount同步拒绝: return_code=-2", return_code=-2)

    monkeypatch.setattr(
        worker_backend_entry, "_resolve_prepared_executor", lambda **_kwargs: _SessionRecoveryExecutor()
    )
    monkeypatch.setattr(worker_backend_entry, "_finalize_executor", lambda _executor: None)
    monkeypatch.setattr(worker_backend_entry, "_append_trade_pre_execute_audit", lambda **_kwargs: None)
    monkeypatch.setattr(worker_responses.logger, "exception", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker_backend_entry, "_append_failed_audit", lambda **_kwargs: None)

    standard_input = UnifiedStandardInput.from_dict(
        {
            "channel_type": TradeChannel.CTP.value,
            "account_config": account.account_config,
            "curr_target": {"ag2612": 0.12},
            "last_target": {},
            "algorithm": {"method": "SINGLE-MAKER", "params": {}},
            "trade_rules": account.trade_rules,
        }
    )
    request = WorkerBackendRequest(
        request_id="req-session-recovery",
        command="execute_trade",
        account_payload=account.model_dump(mode="json"),
        execution_id="exec-session-recovery",
        payload={
            "standard_input": standard_input.to_dict(),
            "audit_input": {},
            "trigger_source": "manual",
            "cleanup": True,
        },
    )

    response = worker_backend_entry._handle_execute_trade(request, worker_backend_entry._WorkerBackendState())

    assert response.kind == "error"
    assert response.error is not None
    assert response.error.type == "ctp_session_recovery_required"
    assert response.error.retryable is True


def test_handle_execute_trade_preserves_original_error_when_failed_audit_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败审计二次异常不得覆盖原始交易异常。"""
    account = _build_gm_account()

    class _BoomExecutor(_FakeWorkerExecutor):
        def execute(
            self,
            standard_input: UnifiedStandardInput,
            *,
            cleanup: bool = True,  # noqa: FBT001, FBT002
            retain_runtime: bool = False,  # noqa: FBT001, FBT002
        ) -> UnifiedStandardOutput:
            _ = standard_input, cleanup, retain_runtime
            raise RuntimeError("original boom")

    monkeypatch.setattr(worker_backend_entry, "_resolve_prepared_executor", lambda **_kwargs: _BoomExecutor())
    monkeypatch.setattr(worker_backend_entry, "_finalize_executor", lambda _executor: None)
    monkeypatch.setattr(worker_backend_entry, "_append_trade_pre_execute_audit", lambda **_kwargs: None)
    monkeypatch.setattr(worker_responses.logger, "exception", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker_responses.logger, "opt", lambda **_kwargs: worker_responses.logger)

    def raise_audit_error(**_kwargs: object) -> None:
        raise OSError("audit boom")

    monkeypatch.setattr(worker_backend_entry, "_append_failed_audit", raise_audit_error)
    standard_input = UnifiedStandardInput.from_dict(
        {
            "channel_type": TradeChannel.GM.value,
            "account_config": account.account_config,
            "curr_target": {},
            "last_target": {},
            "algorithm": {"method": "SINGLE-MAKER", "params": {}},
            "trade_rules": account.trade_rules,
        }
    )
    request = WorkerBackendRequest(
        request_id="req-original-error",
        command="execute_trade",
        account_payload=account.model_dump(mode="json"),
        execution_id="exec-original-error",
        payload={
            "standard_input": standard_input.to_dict(),
            "audit_input": {},
            "trigger_source": "manual",
            "cleanup": True,
        },
    )

    response = worker_backend_entry._handle_execute_trade(request, worker_backend_entry._WorkerBackendState())

    assert response.error is not None
    assert response.error.message == "worker 执行失败，具体原因未确认"


def test_worker_loop_returns_structured_error_for_uncaught_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 最外层应回传未被命令处理器捕获的 BaseException。"""
    request = WorkerBackendRequest.shutdown("req-interrupted", reason="test")

    class _Connection:
        def __init__(self) -> None:
            self.sent: list[WorkerBackendResponse] = []
            self.closed = False

        def recv(self) -> WorkerBackendRequest:
            return request

        def send(self, response: WorkerBackendResponse) -> None:
            self.sent.append(response)

        def close(self) -> None:
            self.closed = True

    connection = _Connection()

    class _ControlConnection:
        def recv(self) -> None:
            raise EOFError

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        worker_backend_entry,
        "_handle_worker_request",
        lambda _request, _state: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(worker_backend_entry, "_close_executor", lambda _executor: None)

    worker_backend_entry.run_worker_backend_loop(  # type: ignore[arg-type]
        connection,
        account_id=2,
        control_connection=_ControlConnection(),
    )

    assert connection.closed is True
    assert len(connection.sent) == 1
    assert connection.sent[0].request_id == "req-interrupted"
    assert connection.sent[0].error is not None
    assert connection.sent[0].error.type == "KeyboardInterrupt"
    assert connection.sent[0].error.message == "KeyboardInterrupt()"


def test_handle_calculate_portfolio_marks_context_account_ctp_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    account = build_account(id=2)

    class _SessionRecoveryExecutor(_FakeWorkerExecutor):
        def get_account_assets(self) -> UnifiedAccountAssets:
            raise CtpSessionRecoveryRequired("ReqQryTradingAccount同步拒绝: return_code=-2", return_code=-2)

    monkeypatch.setattr(
        worker_backend_entry, "_resolve_prepared_executor", lambda **_kwargs: _SessionRecoveryExecutor()
    )
    monkeypatch.setattr(worker_backend_entry, "_finalize_executor", lambda _executor: None)
    request = WorkerBackendRequest(
        request_id="req-portfolio-session-recovery",
        command="calculate_portfolio",
        account_payload=account.model_dump(mode="json"),
        execution_id="exec-portfolio-session-recovery",
        payload={"code": "def calculate_portfolio(context):\n    return {'cash': context.account.available_cash}\n"},
    )

    response = worker_backend_entry._handle_calculate_portfolio(request, worker_backend_entry._WorkerBackendState())

    assert response.kind == "error"
    assert response.channel_type == TradeChannel.CTP
    assert response.error is not None
    assert response.error.type == "ctp_session_recovery_required"
    assert response.error.retryable is True


def test_portfolio_watchdog_exits_stuck_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_codes: list[int] = []
    monkeypatch.setattr(worker_backend_entry.os, "_exit", exit_codes.append)

    worker_backend_entry._terminate_stuck_portfolio_calculation(threading.Event(), 0.0)

    assert exit_codes == [124]
