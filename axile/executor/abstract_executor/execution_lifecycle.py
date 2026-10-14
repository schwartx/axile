"""`AbstractExecutor` 的 execution 生命周期实现.

本模块只承载 `execute()` 主流程、执行编排器构造与
execution 内部共享查询入口。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, cast

from axile.executor.abstract_executor.execution_runtime_facade import (
    AbstractExecutorExecutionRuntimeFacadeMixin,
)
from axile.executor.abstract_executor.execution_runtime_host import _executor
from axile.executor.execution_engine import ExecutionEngine
from axile.executor.execution_query_runtime import ExecutionQueryRuntime
from axile.executor.feishu_notifications import (
    FeishuNotificationSource,
    build_execution_notification_context,
    enqueue_execute_results_to_feishu,
)
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.termination import ExecutionTerminated

if TYPE_CHECKING:
    from axile.executor.models.unified_order import TradeRecord, UnifiedOrder
    from axile.executor.models.unified_output import UnifiedStandardOutput


class AbstractExecutorExecutionLifecycleMixin(AbstractExecutorExecutionRuntimeFacadeMixin):
    """
    承载 execution 生命周期与内部共享查询实现.

    Notes
    -----
    该 mixin 只负责：

    - `execute()` 主流程
    - `ExecutionEngine` 构造
    - execution 内部共享查询 fetcher

    阅读时可以把它当成统一执行骨架：
    连接检查 -> runtime 重置 -> 交易时间判断 -> 输入校验 -> 交给 `ExecutionEngine`
    -> 异步通知 -> 清理/释放 runtime。
    """

    def execute(
        self,
        standard_input: UnifiedStandardInput,
        cleanup: bool = True,
        retain_runtime: bool = False,
        notification_snapshot: dict[str, object] | None = None,
    ) -> UnifiedStandardOutput:
        """
        执行统一交易流程入口.

        Parameters
        ----------
        standard_input : UnifiedStandardInput
            本次执行使用的统一输入对象。
        cleanup : bool, default=True
            执行结束后是否调用资源清理逻辑。
        retain_runtime : bool, default=False
            执行结束后是否保留当前 execution runtime。

        Returns
        -------
        UnifiedStandardOutput
            本次执行返回的统一输出对象。

        Raises
        ------
        TypeError
            当输入对象类型不匹配时抛出。
        ExecutionTerminated
            当执行在检查点被协作式终止时抛出。
        Exception
            当执行过程出现未处理异常时继续向上抛出。
        """
        executor = _executor(self)
        try:
            # 入口层只做 execution 级前置检查，不直接处理 symbol 级策略细节。
            if not isinstance(standard_input, executor._unified_input_type()):
                raise TypeError("standard_input 必须是 UnifiedStandardInput")

            standard_input = executor._normalize_standard_input(standard_input)

            executor.logger.debug("开始执行交易")
            executor._ensure_connection()
            # 总超时的计时零点：连接已就绪、planning 尚未开始，因此执行器构造与登录耗时
            # 不计入额度（要覆盖登录挂死需要另一个 pre-flight 超时）。
            executor._reset_execution_state(standard_input)
            executor._prepare_instrument_catalog(standard_input)
            standard_input = executor._normalize_connected_standard_input(standard_input)

            # 非交易时间直接在入口层返回阻断结果，避免继续进入规划和下单阶段。
            if not executor._check_trading_time():
                executor.logger.warning("非交易时段")
                output = executor._create_non_trading_output(standard_input)
            else:
                executor._validate_input(standard_input)
                # 真正的 symbol 级 planning / dispatch 全部下沉给通用编排层。
                output = executor._execution_engine().run(standard_input)

            # 通知是尾部副作用，不影响主执行结果的返回；投递到有界后台队列，
            # 统一经有界后台队列发送，隔离飞书网络延迟且避免线程随执行次数增长。
            if standard_input.extra.get("server_notification"):
                try:
                    output.extra["server_notification_context"] = build_execution_notification_context(
                        cast("FeishuNotificationSource", executor), output, notification_snapshot
                    )
                except Exception as exc:
                    executor.logger.error(f"保存服务端通知快照失败: {exc}")
            elif standard_input.execution_notification_code:
                executor.logger.info("异步处理执行通知")
                enqueue_execute_results_to_feishu(
                    cast("FeishuNotificationSource", executor),
                    output,
                    standard_input.feishu_key,
                    standard_input.execution_notification_code,
                    notification_snapshot,
                )

            executor.logger.debug(f"交易执行完成 - 成功: {output.success}")
            return output

        except ExecutionTerminated:
            executor.logger.info("执行在检查点终止")
            raise
        except Exception as exc:
            executor.logger.error(f"执行失败: {exc}")
            raise
        finally:
            executor._release_instrument_catalog()
            # cleanup 与 runtime 生命周期分开控制，便于长连接或排障场景单独保留 runtime。
            if cleanup:
                executor._cleanup()
            if not retain_runtime:
                executor.clear_execution_runtime()

    def _execution_engine(self) -> ExecutionEngine:
        """创建本次执行使用的通用编排器."""
        executor = _executor(self)
        return ExecutionEngine(executor, executor.require_execution_runtime())

    def _build_execution_query_runtime(self) -> ExecutionQueryRuntime:
        """创建本次 execution 共享的查询运行时."""
        executor = _executor(self)
        return ExecutionQueryRuntime(
            fetch_pending_orders_snapshot=executor._get_execution_pending_orders_snapshot_fetcher(),
            fetch_pending_orders_by_symbol=executor._get_execution_pending_orders_by_symbol_fetcher(),
            fetch_trades_snapshot=executor._get_execution_trades_snapshot_fetcher(),
            fetch_trades_by_order=executor._get_execution_trades_by_order_fetcher(),
        )

    def _get_execution_pending_orders_snapshot_fetcher(
        self,
    ) -> Callable[[], list[UnifiedOrder]] | None:
        """返回 execution 级挂单 snapshot fetcher；默认不提供."""
        return None

    def _get_execution_pending_orders_by_symbol_fetcher(
        self,
    ) -> Callable[[str], list[UnifiedOrder]] | None:
        """返回 execution 级按 symbol 挂单查询 fetcher."""
        executor = _executor(self)
        return lambda symbol: executor._run_execution_shared_fetch(
            operation="query_order",
            shared_query_key=("query_order", symbol),
            query_scope="narrow",
            symbol=symbol,
            fetcher=lambda: executor._get_pending_orders_impl(symbol),
        )

    def _get_execution_trades_snapshot_fetcher(
        self,
    ) -> Callable[[], list[TradeRecord]] | None:
        """返回 execution 级成交 snapshot fetcher；默认不提供."""
        return None

    def _get_execution_trades_by_order_fetcher(
        self,
    ) -> Callable[[str, str], list[TradeRecord]] | None:
        """返回 execution 级按订单成交查询 fetcher."""
        executor = _executor(self)
        return lambda symbol, order_id: executor._run_execution_shared_fetch(
            operation="query_trades",
            shared_query_key=("query_trades", symbol, order_id),
            query_scope="narrow",
            symbol=symbol,
            metadata={"order_id": order_id},
            fetcher=lambda: executor._query_trades_impl(symbol, order_id),
            success_outcome="fetched",
        )

    def _get_pending_orders_for_execution(self, symbol: str) -> list[UnifiedOrder]:
        """execution-internal 挂单查询入口."""
        executor = _executor(self)
        symbol = executor._normalize_symbol(symbol)
        return cast("list[UnifiedOrder]", executor.require_execution_runtime().get_pending_orders_for_execution(symbol))

    def _query_trades_for_execution(self, symbol: str, order_id: str) -> list[TradeRecord]:
        """execution-internal 成交查询入口."""
        executor = _executor(self)
        symbol = executor._normalize_symbol(symbol)
        return cast(
            "list[TradeRecord]",
            executor.require_execution_runtime().query_trades_for_execution(symbol, order_id),
        )

    def _run_execution_shared_fetch[R](
        self,
        *,
        operation: str,
        shared_query_key: tuple[str, ...],
        query_scope: str,
        fetcher: Callable[[], R],
        symbol: str | None = None,
        metadata: Mapping[str, object] | None = None,
        success_outcome: str = "fetched",
    ) -> R:
        """向 runtime 委托 execution 内部共享查询控制."""
        executor = _executor(self)
        return executor.require_execution_runtime().run_controlled_shared_fetch(
            operation=operation,
            shared_query_key=shared_query_key,
            query_scope=query_scope,
            fetcher=fetcher,
            symbol=symbol,
            metadata=metadata,
            success_outcome=success_outcome,
        )

    def _unified_input_type(self) -> type[UnifiedStandardInput]:
        """为 execute 提供可覆写的输入类型检查入口."""
        from axile.executor.models.unified_input import UnifiedStandardInput

        return UnifiedStandardInput
