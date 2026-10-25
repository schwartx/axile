"""自定义组合函数执行与返回契约测试."""

from __future__ import annotations

import asyncio
from multiprocessing import get_context

import pytest

from axile.executor.ctp.ctp_execute import CtpSessionRecoveryRequired
from axile.server import portfolio_targets
from axile.server.context import Context, build_sample_context
from axile.server.execution.worker_backend import manager as worker_manager_module
from axile.server.portfolio_runner import calculate_sample_portfolio
from axile.server.portfolio_targets import PortfolioFunctionError, calculate_portfolio_target
from tests.unit.server._execution_test_support import build_account


def _calculate(code: str) -> dict[str, float]:
    return calculate_portfolio_target(code, build_sample_context())


def test_function_runs_in_normal_python_environment() -> None:
    result = _calculate(
        "import math\ndef calculate_portfolio(context):\n    return {'rb2610': math.sqrt(context.get_price('rb2610')) / 10}\n"
    )
    assert result == {"rb2610": 1.0}


def test_function_can_use_sample_account() -> None:
    result = _calculate("def calculate_portfolio(context):\n    return {'cash': context.account.available_cash}\n")
    assert result == {"cash": 800_000.0}


@pytest.mark.parametrize(
    ("code", "error_type", "line"),
    [
        ("def calculate_portfolio(context)\n    return {}\n", "SyntaxError", 1),
        ("portfolio = {}\n", "ValueError", None),
        ("def calculate_portfolio():\n    return {}\n", "TypeError", None),
        ("def calculate_portfolio(context):\n    raise RuntimeError('boom')\n", "RuntimeError", 2),
    ],
)
def test_function_errors_are_structured(code: str, error_type: str, line: int | None) -> None:
    with pytest.raises(PortfolioFunctionError) as caught:
        _calculate(code)
    assert caught.value.error_type == error_type
    assert caught.value.error_line == line
    assert caught.value.formatted_traceback


@pytest.mark.parametrize(
    ("expression", "error_type"),
    [
        ("[]", "TypeError"),
        ("{1: 0.5}", "TypeError"),
        ("{'x': True}", "TypeError"),
        ("{'x': float('nan')}", "ValueError"),
        ("{'x': float('inf')}", "ValueError"),
    ],
)
def test_invalid_target_is_rejected(expression: str, error_type: str) -> None:
    with pytest.raises(PortfolioFunctionError) as caught:
        _calculate(f"def calculate_portfolio(context):\n    return {expression}\n")
    assert caught.value.error_type == error_type


def test_function_propagates_ctp_session_recovery_from_context_account() -> None:
    class _RecoveryExecutor:
        def get_account_assets(self):
            raise CtpSessionRecoveryRequired("ReqQryTradingAccount同步拒绝: return_code=-2", return_code=-2)

    with pytest.raises(CtpSessionRecoveryRequired, match="return_code=-2"):
        calculate_portfolio_target(
            "def calculate_portfolio(context):\n    return {'cash': context.account.available_cash}\n",
            Context(_RecoveryExecutor()),  # type: ignore[arg-type]
        )


def _send_structured_error(connection):
    try:
        connection.send(
            PortfolioFunctionError(
                "failure",
                error_line=7,
                error_offset=3,
                error_type="SyntaxError",
                error_message="original",
                formatted_traceback="frozen traceback",
                error_scope="account_connection",
            )
        )
    finally:
        connection.close()


def test_structured_error_survives_real_spawn_pipe():
    context = get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_send_structured_error, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "child did not return its structured error"
        restored = parent.recv()
        assert isinstance(restored, PortfolioFunctionError)
        assert restored.args == ("failure",)
        assert restored.__dict__ == {
            "error_line": 7,
            "error_offset": 3,
            "error_type": "SyntaxError",
            "error_message": "original",
            "formatted_traceback": "frozen traceback",
            "error_scope": "account_connection",
        }
        process.join(timeout=2)
        assert process.exitcode == 0
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)


def test_sample_runner_preserves_script_error_location():
    with pytest.raises(PortfolioFunctionError) as caught:
        calculate_sample_portfolio("def calculate_portfolio(context)\n    return {}\n")
    assert caught.value.error_type == "SyntaxError" and caught.value.error_line == 1
    assert caught.value.error_offset is not None
    assert "SyntaxError" in caught.value.formatted_traceback


def test_sample_runner_executes_in_disposable_process() -> None:
    result = calculate_sample_portfolio(
        "import os\ndef calculate_portfolio(context):\n    return {'pid': float(os.getpid())}\n"
    )
    assert result["pid"] != float(__import__("os").getpid())


def test_sample_runner_terminates_infinite_loop() -> None:
    with pytest.raises(PortfolioFunctionError) as caught:
        calculate_sample_portfolio("def calculate_portfolio(context):\n    while True:\n        pass\n", timeout=0.2)
    assert caught.value.error_type == "TimeoutError"


def test_sample_runner_survives_child_os_exit() -> None:
    with pytest.raises(PortfolioFunctionError) as caught:
        calculate_sample_portfolio("import os\ndef calculate_portfolio(context):\n    os._exit(7)\n", timeout=2.0)
    assert "exitcode=7" in caught.value.error_message


def test_real_account_always_delegates_to_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    account = build_account(id=2, trade_channel="plugin-thread")
    expected = {"x": 1.0}

    class _Manager:
        async def calculate_portfolio(self, actual_account, code: str, *, execution_id: str | None = None):
            assert actual_account is account
            assert code == "code"
            assert execution_id == "exec-1"
            return expected

    monkeypatch.setattr(worker_manager_module, "get_worker_backend_manager", lambda: _Manager())
    result = asyncio.run(portfolio_targets.calculate_portfolio_for_account(account, "code", execution_id="exec-1"))
    assert result is expected
