"""服务端 execution 测试的共享伪对象与辅助函数。"""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace, TracebackType

from axile.common.trade_channel import TradeChannel
from axile.executor.models.execution_result import AlgorithmResult
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import ExecutionStatus, UnifiedStandardOutput
from axile.server.db.models import Account, ExecutionArtifact
from axile.server.execution.audit_batch import ExecutionAuditBatch


def capture_worker_audit_batch(
    *,
    artifact: Callable[..., object] = lambda **_kwargs: None,
    event: Callable[..., object] = lambda **_kwargs: None,
) -> Callable[[ExecutionAuditBatch], int]:
    """在显式批次提交边界捕获 worker 审计，不触碰数据库或生产写入函数。"""

    def capture(batch: ExecutionAuditBatch) -> int:
        for entry in batch:
            callback = artifact if isinstance(entry, ExecutionArtifact) else event
            callback(**entry.model_dump())
        return len(batch)

    return capture


class FakeSession:
    """用于替代异步 Session 的最小测试桩。"""

    record: object | None

    def __init__(self, record: object | None = None) -> None:
        self.record = record

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def execute(self, _statement: object) -> SimpleNamespace:
        return SimpleNamespace(
            scalar_one_or_none=lambda: self.record,
            scalars=lambda: [],
        )


class AccountSession:
    """用于按账户 ID 回放 ``session.get`` 的测试桩。"""

    account: Account | None

    def __init__(self, account: Account | None) -> None:
        self.account = account

    async def __aenter__(self) -> "AccountSession":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def get(self, _model: object, account_id: int) -> Account | None:
        if self.account and self.account.id == account_id:
            return self.account
        return None


class FakeExecutor:
    """可记录输入、审计序列与账户控制 guard 的执行器桩。"""

    audit_context: dict[str, object]
    audit_sink: object | None
    _audit_seq: int
    account_control_guard: object | None
    cleanup: bool
    empty_kwargs: dict[str, object]
    execute_input: object | None

    def __init__(self) -> None:
        self.audit_context = {}
        self.audit_sink = None
        self._audit_seq = 0
        self.account_control_guard = None
        self.executed_guard = None
        self.cleanup = True
        self.empty_kwargs = {}
        self.execute_input = None

    def set_audit_context(self, context: dict[str, object]) -> None:
        self.audit_context = dict(context)
        self._audit_seq = 0

    def next_audit_seq(self) -> int:
        self._audit_seq += 1
        return self._audit_seq

    def get_account_assets(self) -> UnifiedAccountAssets:
        """返回执行前账户快照桩（真实来源、无持仓、总权益 1000）。"""
        return UnifiedAccountAssets(
            available_cash=1000.0,
            total_asset=1000.0,
            market_value=0.0,
            positions=[],
        )

    def set_account_control_guard(self, guard: object | None) -> None:
        self.account_control_guard = guard

    def set_audit_sink(self, sink: object | None) -> None:
        self.audit_sink = sink

    def set_termination_controller(self, _controller: object | None) -> None:
        return None

    def execute(
        self,
        _standard_input: object,
        cleanup: bool = True,  # noqa: FBT002
        retain_runtime: bool = False,  # noqa: FBT001, FBT002
    ) -> tuple[UnifiedStandardOutput, dict[str, object] | None]:
        _ = retain_runtime
        self.execute_input = _standard_input
        self.executed_guard = self.account_control_guard
        self.cleanup = cleanup
        return UnifiedStandardOutput(
            account_assets=UnifiedAccountAssets(
                available_cash=1000.0,
                total_asset=1000.0,
                market_value=0.0,
                positions=[],
            ),
            memory={"ts": datetime(2026, 3, 11, 21, 20, 13)},
            inputs=None,
            execution_time=0.1,
            channel_type=TradeChannel.CTP,
            symbol_results={
                "ag2612": AlgorithmResult(
                    symbol="ag2612",
                    algorithm="SINGLE-MAKER",
                    status=ExecutionStatus.SUCCEEDED,
                    orders=[],
                    target_volume=0.1,
                    first_tick=None,
                    memory={"status": "done"},
                )
            },
            status=ExecutionStatus.SUCCEEDED,
            success=True,
        )

    def empty_positions(self, cleanup: bool = True, **kwargs: object) -> UnifiedStandardOutput:  # noqa: FBT002
        self.executed_guard = self.account_control_guard
        self.cleanup = cleanup
        self.empty_kwargs = kwargs
        return UnifiedStandardOutput(
            account_assets=UnifiedAccountAssets(
                available_cash=1000.0,
                total_asset=1000.0,
                market_value=0.0,
                positions=[],
            ),
            memory={"ts": datetime(2026, 3, 11, 21, 20, 13)},
            inputs=None,
            execution_time=0.1,
            channel_type=TradeChannel.CTP,
            symbol_results={},
            status=ExecutionStatus.SUCCEEDED,
            success=True,
        )


class FakeWorkerBackendManager:
    """记录多进程 worker 调用参数的管理器桩。"""

    def __init__(
        self,
        *,
        trade_output: UnifiedStandardOutput | None = None,
        empty_output: UnifiedStandardOutput | None = None,
    ) -> None:
        self.trade_calls: list[dict[str, object]] = []
        self.empty_calls: list[dict[str, object]] = []
        self._trade_output = trade_output
        self._empty_output = empty_output

    async def execute_trade(
        self,
        *,
        account: Account,
        standard_input_dict: dict[str, object],
        audit_input: dict[str, object],
        execution_id: str | None,
        trigger_source: str,
    ) -> UnifiedStandardOutput:
        self.trade_calls.append(
            {
                "account": account,
                "standard_input_dict": standard_input_dict,
                "audit_input": audit_input,
                "execution_id": execution_id,
                "trigger_source": trigger_source,
            }
        )
        if self._trade_output is not None:
            return self._trade_output, None
        standard_input = UnifiedStandardInput.from_dict(standard_input_dict, channel_type=account.trade_channel)
        return FakeExecutor().execute(standard_input, cleanup=False, retain_runtime=True), None

    async def empty_positions(
        self,
        *,
        account: Account,
        empty_kwargs: dict[str, object],
        audit_input: dict[str, object],
        execution_id: str,
    ) -> UnifiedStandardOutput:
        self.empty_calls.append(
            {
                "account": account,
                "empty_kwargs": empty_kwargs,
                "audit_input": audit_input,
                "execution_id": execution_id,
            }
        )
        if self._empty_output is not None:
            return self._empty_output
        return FakeExecutor().empty_positions(cleanup=True, retain_runtime=True, **empty_kwargs)


class WarningLogger:
    """记录 warning 文本的轻量 logger 桩。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str) -> None:
        self.messages.append(message)


def build_account(**overrides: object) -> Account:
    """构造默认测试账户。"""
    payload: dict[str, object] = {
        "id": 1,
        "name": "ctp-sim",
        "market": "期货",
        "trade_channel": TradeChannel.CTP,
        "account_control_preset": "default",
        "account_control_override": None,
        "account_config": {
            "broker_id": "9999",
            "investor_id": "test",
            "password": "test",
            "td_front": "tcp://td:1",
            "md_front": "tcp://md:2",
            "app_id": "app",
            "auth_code": "auth",
        },
        "is_started": True,
        "cron_expr": "3,6,9 * * * *",
        "remark": None,
        "brokerage": "ctp",
        "weight_precision": 0.001,
        "long_leverage": 1.0,
        "short_leverage": 1.0,
        "algorithm": {"method": "SINGLE-MAKER", "params": {}},
        "empty_positions_algorithm": None,
        "trade_rules": {},
        "forbidden_symbols": [],
        "risk_symbols": [],
        "feishu_key": None,
        "portfolio_id": 1,
        "write_empty_record": 0,
    }
    payload.update(overrides)
    return Account(**payload)


def account_settings_values(account: Account) -> dict[str, object]:
    """按 SQLite 夹具的列分组保存扁平账户配置。"""
    values = account.model_dump(mode="json")
    groups = {
        "execution": (
            "account_control_preset",
            "account_control_override",
            "weight_precision",
            "long_leverage",
            "short_leverage",
            "algorithm",
            "empty_positions_algorithm",
            "trade_rules",
            "futures_close_policy",
            "forbidden_symbols",
            "risk_symbols",
            "write_empty_record",
            "execution_timeout",
        ),
        "schedule": ("is_started", "cron_expr", "supplement"),
        "notification": ("feishu_key", "execution_notification_code"),
        "backtest": ("backtest_weight_type", "backtest_fee_rate"),
    }
    return {"connection": values["account_config"]} | {
        group: {name: values[name] for name in fields} for group, fields in groups.items()
    }


def insert_account(connection: sqlite3.Connection, account: Account) -> None:
    """为已迁移的测试库设置显式账户 ID 与分组配置。"""
    values = account.model_dump(mode="json")
    names = (
        "id",
        "name",
        "market",
        "trade_channel",
        "remark",
        "brokerage",
        "portfolio_id",
        "created_at",
        "updated_at",
        "copied_from_account_id",
        "copied_from_account_name",
    )
    connection.execute(
        f"INSERT INTO account ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
        [values.get(name) for name in names],
    )
    settings = account_settings_values(account)
    groups = ("connection", "execution", "schedule", "notification", "backtest")
    connection.execute(
        "INSERT INTO account_settings(account_id,connection,execution,schedule,notification,backtest) VALUES (?,?,?,?,?,?)",
        [account.id, *[json.dumps(settings[group], ensure_ascii=False, allow_nan=False) for group in groups]],
    )


async def noop_account_control_guard(*_args: object, **_kwargs: object) -> object:
    """返回占位 guard。"""
    return object()


async def noop_append_execution_event(**_kwargs: object) -> bool:
    """吞掉 execution event 写入。"""
    return True


async def noop_append_execution_artifact(**_kwargs: object) -> bool:
    """吞掉 execution artifact 写入。"""
    return True


class WorkerCommandHarness:
    """绕过 IPC 运行真实 worker 命令，用于验证命令前后的输入、审计和清理。"""

    def __init__(self):
        from axile.server.execution.worker_backend.worker_state import _WorkerBackendState

        self.state = _WorkerBackendState()

    async def _run(self, command, account, execution_id, payload):
        from axile.server.execution.worker_backend.protocol import WorkerBackendRequest
        from axile.server.execution.worker_backend.worker import _handle_worker_request

        request = WorkerBackendRequest(
            request_id="unit-worker",
            command=command,
            account_payload=account.model_dump(mode="json"),
            execution_id=execution_id,
            payload=payload,
        )
        response = await asyncio.to_thread(_handle_worker_request, request, self.state)
        assert response.kind == "result", response.error
        return UnifiedStandardOutput.model_validate(response.output_payload), response.normalized_symbol_fields

    async def execute_trade(self, *, account, execution_id, standard_input_dict, trigger_source, **kwargs):
        return await self._run(
            "execute_trade",
            account,
            execution_id,
            {
                "standard_input": standard_input_dict,
                "trigger_source": trigger_source,
                "audit_input": kwargs["audit_input"],
            },
        )

    async def empty_positions(self, *, account, execution_id, empty_kwargs, audit_input, **kwargs):
        output, _ = await self._run(
            "empty_positions",
            account,
            execution_id,
            {
                "empty_kwargs": empty_kwargs,
                "audit_input": audit_input,
            },
        )
        return output
