"""通用执行编排层.

`ExecutionEngine` 只负责编排本次 execution 的 symbol 级工作流；
真正的 execution 状态统一从 `ExecutionRuntime` 读取，不再回写到 `AbstractExecutor`。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING

from axile.domain.execution import ExecutionEventStatus, ExecutionEventType, ExecutionReasonFamily
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.algorithms.core.base import AlgorithmInput, resolve_algorithm
from axile.executor.algorithms.exceptions import execution_error_message
from axile.executor.algorithms.utils.outcome import TARGET_NOT_REACHED, summarize_outcome
from axile.executor.execution_runtime import ExecutionRuntime
from axile.executor.execution_session import ExecutionSession
from axile.executor.market_data_validation import MarketDataUnavailableError
from axile.executor.models.execution_result import (
    AlgorithmResult,
    ExecutionStatus,
    TargetSizingDecision,
    is_success_status,
)
from axile.executor.models.unified_account_assets import UnifiedAccountAssets, is_degraded_snapshot_source
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_order import UnifiedOrder
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.models.unified_price import UnifiedPriceData, clone_price_data
from axile.executor.order_cancellation import CancelNotAllowedError
from axile.executor.replay import observe, replay_enabled
from axile.executor.session_closed import COMMON_SESSION_CLOSED, normal_session_skip
from axile.executor.termination import ExecutionTerminated

if TYPE_CHECKING:
    from axile.executor.abstract_executor.base import AbstractExecutor


type ObjectDict = dict[str, object]
type AuditContext = ObjectDict
type OrderAuditMetadata = ObjectDict
type TradeRule = ObjectDict
type TargetVolumeValue = int | float


# 品种执行状态 → 审计事件状态的映射；成功/无操作记 SUCCESS，部分/阻塞记 WARNING，失败记 ERROR。
_SYMBOL_EVENT_STATUS_MAP: dict[ExecutionStatus, ExecutionEventStatus] = {
    ExecutionStatus.SUCCEEDED: ExecutionEventStatus.SUCCESS,
    ExecutionStatus.NOOP: ExecutionEventStatus.SUCCESS,
    ExecutionStatus.PARTIAL: ExecutionEventStatus.WARNING,
    ExecutionStatus.BLOCKED: ExecutionEventStatus.WARNING,
    ExecutionStatus.FAILED: ExecutionEventStatus.ERROR,
}


# 品种执行状态 → 审计原因族的映射；用于让审计能区分系统失败、账户阻塞与策略性跳过。
_SYMBOL_EVENT_REASON_FAMILY_MAP: dict[ExecutionStatus, ExecutionReasonFamily] = {
    ExecutionStatus.SUCCEEDED: ExecutionReasonFamily.EXECUTION_STRATEGY,
    ExecutionStatus.NOOP: ExecutionReasonFamily.EXECUTION_STRATEGY,
    ExecutionStatus.PARTIAL: ExecutionReasonFamily.SYSTEM,
    ExecutionStatus.BLOCKED: ExecutionReasonFamily.ACCOUNT_STATE,
    ExecutionStatus.FAILED: ExecutionReasonFamily.SYSTEM,
}


def _coerce_object_dict(value: object) -> ObjectDict:
    """将任意字典值收敛为 `dict[str, object]`."""
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _coerce_int(value: object) -> int | None:
    """将常见整型输入安全收敛为 `int`."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _derive_dispatch_status(symbol_results: dict[str, AlgorithmResult]) -> ExecutionStatus:
    """根据按品种结果推导本次执行的整体状态.

    Notes
    -----
    账户级结论按「最重的事实」表述：任何品种有执行进展但未完成（PARTIAL），
    账户就是 PARTIAL——即使另有品种彻底失败，因为账户确实发生了部分成交/位移，
    判 FAILED 会夸大；品种级的硬失败保留在 symbol_results 与审计计数里可见。
    只有不存在任何 PARTIAL 时才进入 FAILED/BLOCKED 判定：全败（含与 BLOCKED
    混合）即 FAILED，避免「0 成交」被 BLOCKED 稀释。
    """
    statuses = [result.status for result in symbol_results.values()]
    if ExecutionStatus.PARTIAL in statuses:
        return ExecutionStatus.PARTIAL
    if ExecutionStatus.FAILED in statuses:
        if ExecutionStatus.SUCCEEDED not in statuses:
            return ExecutionStatus.FAILED
        return ExecutionStatus.PARTIAL
    if ExecutionStatus.BLOCKED in statuses:
        if any(is_success_status(status) for status in statuses) and _only_session_closed_failures(symbol_results):
            return ExecutionStatus.PARTIAL
        if ExecutionStatus.SUCCEEDED not in statuses:
            return ExecutionStatus.BLOCKED
        return ExecutionStatus.PARTIAL
    if all(status == ExecutionStatus.NOOP for status in statuses):
        return ExecutionStatus.NOOP
    return ExecutionStatus.SUCCEEDED


def _only_session_closed_failures(symbol_results: dict[str, AlgorithmResult]) -> bool:
    """确认所有未完成品种均为尚未下单的休市阻断。"""
    affected = [result for result in symbol_results.values() if not is_success_status(result.status)]
    return bool(affected) and all(
        result.status == ExecutionStatus.BLOCKED
        and result.reason_code == COMMON_SESSION_CLOSED
        and not result.orders
        and not result.trades
        for result in affected
    )


def _derive_dispatch_error(
    status: ExecutionStatus,
    symbol_results: dict[str, AlgorithmResult],
) -> str | None:
    """根据整体状态提取顶层错误信息."""
    failed_results = [result for result in symbol_results.values() if not is_success_status(result.status)]
    if not failed_results:
        return None
    if status == ExecutionStatus.PARTIAL and _only_session_closed_failures(symbol_results):
        return f"部分品种休市，{len(failed_results)} 个品种未处理"
    if len(failed_results) == 1 and failed_results[0].error:
        return failed_results[0].error
    if status == ExecutionStatus.BLOCKED:
        reasons = {result.error for result in failed_results}
        if len(reasons) == 1 and failed_results[0].error:
            return f"{failed_results[0].error}，{len(failed_results)} 个品种未执行"
        return f"{len(failed_results)} 个品种执行受阻"
    return f"{len(failed_results)} 个品种执行未成功"


def _merge_symbol_terminations(terminations: list[ExecutionTerminated]) -> ExecutionTerminated:
    """
    把并行品种各自的终止异常合并成一个.

    Parameters
    ----------
    terminations : list[ExecutionTerminated]
        各品种线程抛出的终止异常，按完成先后排列。

    Returns
    -------
    ExecutionTerminated
        以最先完成的异常为基准，汇总全部撤单操作和订单终态证据。

    Notes
    -----
    撤单操作与订单终态分别保留；只保留一个品种的异常会丢失其余品种的未决委托。
    ``reason`` / ``mode`` / ``trigger`` 在同一次执行内对所有品种同源，取首个即可。
    """
    primary = terminations[0]
    primary.cancel_failed_order_ids = list(
        dict.fromkeys(order_id for termination in terminations for order_id in termination.cancel_failed_order_ids)
    )
    primary.cancel_unconfirmed = any(termination.cancel_unconfirmed for termination in terminations)
    primary.cancel_details = {
        key: reason for termination in terminations for key, reason in termination.cancel_details.items()
    }
    primary.cancel_outcomes = {
        key: outcome for termination in terminations for key, outcome in termination.cancel_outcomes.items()
    }
    attempts = [termination.cancel_attempted for termination in terminations]
    primary.cancel_attempted = True if any(attempts) else (False if all(value is False for value in attempts) else None)
    return primary


@dataclass(frozen=True)
class _PreparedSymbolAlgorithm:
    """按品种调度前预先整理好的执行任务."""

    symbol: str
    algorithm_name: str
    algorithm_input: AlgorithmInput
    audit_context: AuditContext | None = None
    sizing: TargetSizingDecision | None = None


@dataclass(frozen=True)
class _PlannedSymbolAlgorithm:
    """按品种规划后的执行计划."""

    symbol: str
    algorithm_name: str
    params: object | None
    trade_rule: TradeRule
    audit_context: AuditContext | None
    current_volume: float
    final_target_volume: TargetVolumeValue
    sizing: TargetSizingDecision


@dataclass(frozen=True)
class _DispatchPlanningResult:
    """本次按品种调度的规划快照。"""

    plans: list[_PlannedSymbolAlgorithm]
    planning_failures: list[AlgorithmResult]
    account_assets: UnifiedAccountAssets
    market_data: dict[str, UnifiedPriceData]


@dataclass(frozen=True)
class _DispatchDecision:
    """单个品种在两阶段调度中的归类结果。"""

    phase_one_target_volume: TargetVolumeValue | None
    run_phase_two: bool
    is_noop: bool


@dataclass(frozen=True)
class _DispatchPhases:
    """按阶段拆分后的执行任务与直接结果。"""

    phase_one_tasks: list[_PreparedSymbolAlgorithm]
    phase_two_plans: list[_PlannedSymbolAlgorithm]
    direct_results: list[AlgorithmResult]


@dataclass(frozen=True)
class _PhaseTwoReplanResult:
    """第二阶段重算后的任务与规划失败结果。"""

    tasks: list[_PreparedSymbolAlgorithm]
    planning_failures: list[AlgorithmResult]


class ExecutionEngine:
    """
    通用执行编排器.

    Notes
    -----
    阅读本类时可以按以下主线理解：

    1. 基于账户快照和行情为每个 symbol 生成执行计划。
    2. 按“先减后增”拆成两阶段调度，降低资金与方向切换冲突。
    3. 将阶段结果重新合并成 symbol 级 `AlgorithmResult`。
    4. 最终汇总为 `UnifiedStandardOutput`。
    """

    def __init__(self, owner: AbstractExecutor, runtime: ExecutionRuntime | None = None) -> None:
        self._owner = owner
        self._runtime = runtime or owner.require_execution_runtime()
        self._account_snapshot: tuple[UnifiedAccountAssets, str | None] | None = None
        self._resolved_positions: dict[str, float] = {}

    def run(self, standard_input: UnifiedStandardInput) -> UnifiedStandardOutput:
        """执行通用编排流程."""
        self._resolved_positions = {}
        results = self._run_symbol_algorithms(standard_input)
        by_symbol = {result.symbol: result for result in results}
        for symbol, expected in self._resolved_positions.items():
            if symbol not in by_symbol:
                results.append(
                    AlgorithmResult(
                        symbol=symbol,
                        algorithm="POSITION-CONFIRMATION",
                        status=ExecutionStatus.NOOP,
                        final_volume=expected,
                        expected_volume=expected,
                        position_confirmed=True,
                    )
                )
            elif (
                by_symbol[symbol].expected_volume is None
                and not by_symbol[symbol].orders
                and not by_symbol[symbol].trades
            ):
                by_symbol[symbol].expected_volume = expected
                by_symbol[symbol].position_confirmed = True
        for result in results:
            if result.expected_volume is not None and result.position_confirmed is False:
                self._owner.pending_positions[result.symbol] = result.expected_volume
        return self._create_standard_output_from_results(standard_input, results)

    def _run_symbol_algorithm(self, task: _PreparedSymbolAlgorithm) -> AlgorithmResult:
        """执行单个品种的算法任务."""
        session = self._create_symbol_session(task.symbol, task.audit_context)
        session.handle_termination_checkpoint()
        if replay_enabled(self._owner):
            observe(
                self._owner,
                "/execution",
                {
                    "execution_id": (task.audit_context or {}).get("execution_id"),
                    "symbol": task.symbol,
                    "phase": "target",
                    "target_quantity": task.algorithm_input.target_volume,
                    "algorithm": task.algorithm_name,
                },
            )
        return self._run_algorithm_with_session(session, task.algorithm_name, task.algorithm_input)

    def _run_algorithm_with_session(
        self,
        session: ExecutionSession,
        algorithm_name: str,
        algorithm_input: AlgorithmInput,
    ) -> AlgorithmResult:
        """在单品种执行会话上运行算法."""
        algorithm_func = resolve_algorithm(algorithm_name, session)
        result = algorithm_func(session, algorithm_input)
        merged_memory = dict(session.memory)
        merged_memory.update(result.memory)
        result.memory = merged_memory
        result.symbol = session.symbol
        result.algorithm = algorithm_name
        return result

    def _create_symbol_session(
        self,
        symbol: str,
        audit_context: AuditContext | None,
    ) -> ExecutionSession:
        """根据单品种输入创建执行会话."""
        return ExecutionSession(owner=self._owner, runtime=self._runtime, symbol=symbol, audit_context=audit_context)

    def _run_symbol_algorithms(self, standard_input: UnifiedStandardInput) -> list[AlgorithmResult]:
        """按品种规划执行，并按先减后增的顺序分阶段调度."""
        self._account_snapshot = None
        self._owner.handle_termination_checkpoint()
        # planning 阶段只做“算清楚每个 symbol 应该做什么”，不真正触发算法执行。
        self._runtime.report_progress("planning")
        planning = self._build_symbol_algorithm_plans(standard_input)
        pending = standard_input.extra.get("pending_positions", {})
        if not isinstance(pending, dict) or any(
            not isinstance(symbol, str)
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isfinite(value)
            for symbol, value in pending.items()
        ):
            raise ValueError("待核对持仓证据无效")
        self._owner.pending_positions.update(pending)
        blocked = self._pending_position_results(planning.account_assets)
        if blocked:
            self._account_snapshot = planning.account_assets, None
            self._emit_symbol_decision_events(standard_input, blocked)
            return blocked
        # dispatch 阶段把计划拆成可直接返回的 NOOP、第一阶段减仓、第二阶段开仓三类。
        dispatch_phases = self._build_dispatch_phases(planning)
        try:
            self._prepare_account_for_symbol_dispatch(dispatch_phases)
        except CancelNotAllowedError as exc:
            direct_symbols = {result.symbol for result in dispatch_phases.direct_results}
            return [
                *planning.planning_failures,
                *dispatch_phases.direct_results,
                *[
                    self._build_failed_algorithm_result(
                        symbol=plan.symbol,
                        algorithm_name=plan.algorithm_name,
                        error=exc.execution_error,
                        status=ExecutionStatus.BLOCKED,
                        reason_code="COMMON.CANCEL.RESTRICTED",
                        orders=[
                            order
                            for order in self._owner.account_orders.snapshot()["data"]
                            if order.symbol == plan.symbol
                        ],
                        account_assets=planning.account_assets,
                        sizing=plan.sizing,
                    )
                    for plan in planning.plans
                    if plan.symbol not in direct_symbols
                ],
            ]
        if dispatch_phases.phase_one_tasks or dispatch_phases.phase_two_plans:
            self._runtime.report_progress("executing")
        phase_one_results = self._run_phase_one_dispatch(dispatch_phases.phase_one_tasks)
        # 第二阶段只有在第一阶段没有未成功结果时才继续，避免边减仓失败边开新仓。
        phase_two_results = self._run_phase_two_dispatch(
            standard_input=standard_input,
            planning=planning,
            dispatch_phases=dispatch_phases,
            phase_one_results=phase_one_results,
        )
        results = [
            *planning.planning_failures,
            *dispatch_phases.direct_results,
            *phase_one_results,
            *phase_two_results,
        ]
        self._runtime.report_progress("settling")
        merged_results = self._merge_symbol_algorithm_results(results)
        if self._account_snapshot is None:
            merged_results = self._settle_result_positions(merged_results)
        else:
            merged_results = self._reconcile_result_positions(merged_results, self._account_snapshot[0])
        self._emit_symbol_decision_events(standard_input, merged_results)
        return merged_results

    def _pending_position_results(self, assets: UnifiedAccountAssets) -> list[AlgorithmResult]:
        """上一轮仓位未核对时阻断新单，跨执行保留成交推导证据。"""
        blocked = []
        for symbol, expected in list(self._owner.pending_positions.items()):
            actual = self._owner.get_current_volume(symbol, assets)
            if not is_degraded_snapshot_source(assets.source) and self._is_same_volume(actual, expected):
                del self._owner.pending_positions[symbol]
                self._resolved_positions[symbol] = expected
                continue
            blocked.append(
                AlgorithmResult(
                    symbol=symbol,
                    algorithm="POSITION-CONFIRMATION",
                    status=ExecutionStatus.BLOCKED,
                    error="上一轮成交后的持仓尚待核对，已阻断本次下单",
                    reason_code="COMMON.POSITION.UNCONFIRMED",
                    reason_family=ExecutionReasonFamily.ACCOUNT_STATE,
                    expected_volume=expected,
                    position_confirmed=False,
                    account_assets=assets,
                )
            )
        return blocked

    def _run_symbol_algorithms_serially(
        self,
        tasks: list[_PreparedSymbolAlgorithm],
    ) -> list[AlgorithmResult]:
        """在当前执行器实例上串行执行各品种算法."""
        return [
            self._run_symbol_algorithm_with_error_capture(
                task,
                runner=lambda current_task=task: self._run_symbol_algorithm(current_task),
            )
            for task in tasks
        ]

    def _run_symbol_algorithms_in_parallel(
        self,
        tasks: list[_PreparedSymbolAlgorithm],
    ) -> list[AlgorithmResult]:
        """
        在通用调度层并行执行各品种算法.

        Notes
        -----
        终止异常不在循环里直接向上抛，而是先收齐再合并后重抛：每个品种线程都会把
        **自己**那份撤单和订单终态证据挂在自己的终止异常上，一旦提前逃出
        循环，其余 future 上的异常就再也不会被取出，那些品种的游离挂单会在审计里
        彻底消失——记录看着一切正常，实际敞口还挂在盘上。

        这里不额外付出等待成本：``ThreadPoolExecutor`` 的上下文退出本就是
        ``shutdown(wait=True)``，无论如何都要等所有线程返回。
        """
        futures: dict[Future[AlgorithmResult], _PreparedSymbolAlgorithm] = {}
        results: list[AlgorithmResult] = []
        terminations: list[ExecutionTerminated] = []

        max_workers = min(len(tasks), self._owner._max_parallel_symbol_workers())
        self._owner.logger.debug(
            "symbol 并发调度 channel={} tasks={} max_workers={}",
            self._owner.channel_type.value,
            len(tasks),
            max_workers,
        )
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"{self._owner.channel_type.value}-symbol-algo",
        ) as pool:
            for task in tasks:
                futures[pool.submit(self._run_symbol_algorithm, task)] = task

            for future in as_completed(futures):
                task = futures[future]
                try:
                    results.append(
                        self._run_symbol_algorithm_with_error_capture(
                            task,
                            runner=future.result,
                        )
                    )
                except ExecutionTerminated as exc:
                    terminations.append(exc)

        if terminations:
            raise _merge_symbol_terminations(terminations)

        return results

    def _run_symbol_algorithm_with_error_capture(
        self,
        task: _PreparedSymbolAlgorithm,
        runner: Callable[[], AlgorithmResult],
    ) -> AlgorithmResult:
        """
        执行单个品种任务，并将异常统一转换为失败结果.

        Parameters
        ----------
        task : _PreparedSymbolAlgorithm
            当前待执行的品种任务。
        runner : Callable[[], AlgorithmResult]
            实际执行任务的可调用对象。

        Returns
        -------
        AlgorithmResult
            成功时返回算法结果；失败时返回归一化后的错误结果。
        """
        try:
            result = runner()
            result.sizing = task.sizing
            conversion = task.sizing.value_conversion if task.sizing is not None else None
            if conversion is not None:
                # 前后阶段可使用不同规划快照，换算依据必须逐笔保留，不能在合并后补填。
                for record in [*result.orders, *result.trades]:
                    record.value_conversion = conversion.model_copy(deep=True)
            return result
        except ExecutionTerminated:
            # 协作式终止不是失败：必须穿透 symbol 级错误捕获，交给上层 lifecycle
            # 记录为 TERMINATED（携带 reason/mode），否则会被归一化成「执行失败：未知原因」。
            raise
        except AccountControlBlockedError as exc:
            return self._build_failed_algorithm_result(
                symbol=task.symbol,
                algorithm_name=task.algorithm_name,
                error=exc.execution_error,
                status=ExecutionStatus.BLOCKED,
                reason_code=exc.reason_code,
                reason_family=ExecutionReasonFamily.ACCOUNT_STATE,
                sizing=task.sizing,
            )
        except MarketDataUnavailableError as exc:
            return self._build_failed_algorithm_result(
                symbol=task.symbol,
                algorithm_name=task.algorithm_name,
                error=exc.execution_error,
                status=ExecutionStatus.BLOCKED,
                reason_code=exc.reason_code,
                reason_family=ExecutionReasonFamily.MARKET_RULE,
                sizing=task.sizing,
            )
        except Exception as exc:
            if bool(getattr(exc, "requires_session_recovery", False)):
                raise
            return self._build_failed_algorithm_result(
                symbol=task.symbol,
                algorithm_name=task.algorithm_name,
                error=execution_error_message(exc),
                sizing=task.sizing,
            )

    def _run_prepared_symbol_algorithms(
        self,
        tasks: list[_PreparedSymbolAlgorithm],
    ) -> list[AlgorithmResult]:
        """根据执行器能力选择串行或并行执行预先规划好的任务."""
        if not tasks:
            return []
        symbols = list(dict.fromkeys(task.symbol for task in tasks))
        self._owner.initialize_websocket(symbols)
        if self._owner._supports_parallel_symbol_dispatch():
            return self._run_symbol_algorithms_in_parallel(tasks)
        return self._run_symbol_algorithms_serially(tasks)

    def _symbol_session_block(self, symbol: str) -> tuple[str, str] | None:
        """返回该品种因交易时段不能下单时的 ``(reason_code, message)``；可下单则 ``None``.

        Notes
        -----
        时段只拦 dispatch，不拦换算。默认渠道没有品种级时段。
        """
        _ = symbol
        return None

    def _build_symbol_algorithm_plans(
        self,
        standard_input: UnifiedStandardInput,
    ) -> _DispatchPlanningResult:
        """为本次执行规划按品种算法计划."""
        account_assets, effective_curr_target, symbols = self._build_symbol_planning_context(standard_input)
        return self._build_symbol_algorithm_plans_for_symbols(
            standard_input=standard_input,
            account_assets=account_assets,
            symbols=symbols,
            effective_curr_target=effective_curr_target,
        )

    def _build_symbol_planning_context(
        self,
        standard_input: UnifiedStandardInput,
    ) -> tuple[UnifiedAccountAssets, dict[str, float], list[str]]:
        """取得通用 planning 使用的账户快照、目标权重与候选 symbol。"""
        account_assets = self._owner.get_account_assets()
        effective_curr_target = self._build_effective_curr_target(standard_input, account_assets)
        symbols = self._owner.get_all_symbols(effective_curr_target, standard_input.last_target)
        # 空仓目标且没有待处理品种时，沿空计划汇总为 NOOP。
        return account_assets, effective_curr_target, symbols

    def _get_symbol_algorithm_name(self, standard_input: UnifiedStandardInput, symbol: str) -> str:
        """解析单个 symbol 最终使用的算法名称。"""
        resolved_algorithm = standard_input.get_resolved_symbol_algorithm(symbol)
        return str(resolved_algorithm.get("method", standard_input.algorithm.get("method", "SINGLE-MAKER")))

    def _build_symbol_algorithm_plans_for_symbols(
        self,
        *,
        standard_input: UnifiedStandardInput,
        account_assets: UnifiedAccountAssets,
        symbols: list[str],
        effective_curr_target: dict[str, float],
        planning_failures: list[AlgorithmResult] | None = None,
    ) -> _DispatchPlanningResult:
        """为明确给定的 symbol 集合组装通用算法计划。"""
        planning_failures = list(planning_failures or [])
        planning_market_data, sizing_decisions = self._plan_target_volumes_for_symbols(
            standard_input=standard_input,
            account_assets=account_assets,
            symbols=symbols,
            effective_curr_target=effective_curr_target,
        )
        plans: list[_PlannedSymbolAlgorithm] = []
        for symbol in symbols:
            planned = self._plan_or_block_symbol(
                symbol=symbol,
                standard_input=standard_input,
                account_assets=account_assets,
                effective_curr_target=effective_curr_target,
                planning_market_data=planning_market_data,
                sizing_decisions=sizing_decisions,
            )
            if isinstance(planned, AlgorithmResult):
                planning_failures.append(planned)
            else:
                plans.append(planned)
        return _DispatchPlanningResult(
            plans=plans,
            planning_failures=planning_failures,
            account_assets=account_assets,
            market_data=planning_market_data,
        )

    def _plan_or_block_symbol(
        self,
        *,
        symbol: str,
        standard_input: UnifiedStandardInput,
        account_assets: UnifiedAccountAssets,
        effective_curr_target: dict[str, float],
        planning_market_data: dict[str, UnifiedPriceData],
        sizing_decisions: dict[str, TargetSizingDecision],
    ) -> _PlannedSymbolAlgorithm | AlgorithmResult:
        """先换算，再按时段决定是计划下单还是 BLOCKED 带走证据."""
        resolved_algorithm = standard_input.get_resolved_symbol_algorithm(symbol)
        algorithm_name = self._get_symbol_algorithm_name(standard_input, symbol)
        sizing = sizing_decisions.get(symbol)
        target_volume = sizing.target_quantity if sizing is not None else None
        session_block = self._symbol_session_block(symbol)
        if session_block is not None:
            reason_code, message = session_block
            if sizing is not None:
                sizing.current_quantity = self._owner.get_current_volume(symbol, account_assets)
            retained = [order for order in self._owner.account_orders.snapshot()["data"] if order.symbol == symbol]
            normal_skip = reason_code == COMMON_SESSION_CLOSED and not retained
            return self._build_failed_algorithm_result(
                symbol=symbol,
                algorithm_name=algorithm_name,
                error=None if normal_skip else message,
                outcome_reason=message,
                status=ExecutionStatus.NOOP if normal_skip else ExecutionStatus.BLOCKED,
                orders=retained,
                account_assets=account_assets,
                target_volume=target_volume,
                first_tick=clone_price_data(planning_market_data.get(symbol)),
                reason_code=reason_code,
                reason_family=ExecutionReasonFamily.MARKET_RULE,
                sizing=sizing,
            )
        if sizing is None or target_volume is None:
            return self._build_failed_algorithm_result(
                symbol=symbol,
                algorithm_name=algorithm_name,
                error=self._build_target_volume_planning_error(
                    symbol,
                    effective_curr_target,
                    planning_market_data,
                ),
                account_assets=account_assets,
                first_tick=clone_price_data(planning_market_data.get(symbol)),
                sizing=sizing,
                status=(
                    ExecutionStatus.BLOCKED
                    if sizing is not None
                    and (sizing.reason_code == "COMMON.MARKET_DATA.UNAVAILABLE" or sizing.sizing_mode == "quantity")
                    else ExecutionStatus.FAILED
                ),
                reason_code=sizing.reason_code if sizing is not None else None,
                reason_family=(
                    ExecutionReasonFamily.MARKET_RULE
                    if sizing is not None
                    and (sizing.reason_code == "COMMON.MARKET_DATA.UNAVAILABLE" or sizing.sizing_mode == "quantity")
                    else None
                ),
            )
        current_volume = self._owner.get_current_volume(symbol, account_assets)
        sizing.current_quantity = current_volume
        return _PlannedSymbolAlgorithm(
            symbol=symbol,
            algorithm_name=algorithm_name,
            params=resolved_algorithm.get("params"),
            trade_rule=dict(standard_input.trade_rules.get(symbol, {})),
            audit_context=self._build_symbol_audit_context(standard_input, symbol, algorithm_name),
            current_volume=current_volume,
            final_target_volume=target_volume,
            sizing=sizing,
        )

    def _build_effective_curr_target(
        self,
        standard_input: UnifiedStandardInput,
        account_assets: UnifiedAccountAssets,
    ) -> dict[str, float]:
        """基于账户快照应用风控后的有效目标权重."""
        return account_assets.update_curr_target(
            standard_input.curr_target,
            standard_input.forbidden_symbols,
            standard_input.risk_symbols,
        )

    def _planning_market_data(self, symbols: list[str]) -> dict[str, UnifiedPriceData]:
        """可下单品种走阻塞行情；时段阻断品种只 peek 缓存，避免 CTP 等全列表合法盘口."""
        blocking = [symbol for symbol in symbols if self._symbol_session_block(symbol) is None]
        blocked = [symbol for symbol in symbols if symbol not in set(blocking)]
        market_data = dict(self._owner.get_market_data(blocking)) if blocking else {}
        cached = getattr(self._owner, "_quotes", None)
        if not isinstance(cached, dict):
            return market_data
        for symbol in blocked:
            quote = cached.get(symbol)
            if quote is not None and symbol not in market_data:
                market_data[symbol] = quote
        return market_data

    def _plan_target_volumes_for_symbols(
        self,
        *,
        standard_input: UnifiedStandardInput,
        account_assets: UnifiedAccountAssets,
        symbols: list[str],
        effective_curr_target: dict[str, float] | None = None,
    ) -> tuple[dict[str, UnifiedPriceData], dict[str, TargetSizingDecision]]:
        """基于指定账户快照为一组品种规划目标数量."""
        if not symbols:
            return {}, {}

        current_target = effective_curr_target or self._build_effective_curr_target(standard_input, account_assets)
        scoped_curr_target = {symbol: current_target[symbol] for symbol in symbols if symbol in current_target}
        scoped_last_target = {
            symbol: weight for symbol, weight in standard_input.last_target.items() if symbol in set(symbols)
        }
        trade_rules = standard_input.trade_rules
        if standard_input.target_mode == "quantity":
            trade_rules = {symbol: {**trade_rules.get(symbol, {}), "sizing_mode": "quantity"} for symbol in symbols}
        quote_symbols = self._planning_quote_symbols(symbols, scoped_curr_target, account_assets, trade_rules)
        planning_market_data = self._planning_market_data(quote_symbols)
        sizing_decisions = self._owner.calculate_target_sizing(
            scoped_curr_target,
            account_assets,
            planning_market_data,
            trade_rules,
            scoped_last_target,
            standard_input.forbidden_symbols,
        )
        return planning_market_data, sizing_decisions

    @staticmethod
    def _planning_quote_symbols(
        symbols: list[str], targets: dict[str, float], assets: UnifiedAccountAssets, rules: dict[str, dict[str, object]]
    ) -> list[str]:
        """仅跳过可信快照中零目标且没有任何方向持仓的定手数合约。"""
        if is_degraded_snapshot_source(assets.source):
            return symbols
        held = {position.symbol for position in assets.positions if position.volume != 0}
        return [
            symbol
            for symbol in symbols
            if symbol in held
            or targets.get(symbol, 0) != 0
            or rules.get(symbol, {}).get("sizing_mode", "weight") not in {"lots", "quantity"}
        ]

    def _classify_symbol_dispatch_phases(
        self,
        plan: _PlannedSymbolAlgorithm,
    ) -> _DispatchDecision:
        """判断单个品种属于哪个阶段，并返回阶段目标."""
        current_volume = plan.current_volume
        final_target_volume = plan.final_target_volume

        if self._is_same_volume(current_volume, final_target_volume):
            return _DispatchDecision(phase_one_target_volume=None, run_phase_two=False, is_noop=True)

        if self._is_zero_volume(current_volume):
            return _DispatchDecision(
                phase_one_target_volume=None,
                run_phase_two=not self._is_zero_volume(final_target_volume),
                is_noop=False,
            )

        if self._is_zero_volume(final_target_volume):
            return _DispatchDecision(phase_one_target_volume=0, run_phase_two=False, is_noop=False)

        if self._is_same_direction(current_volume, final_target_volume):
            if abs(final_target_volume) < abs(current_volume):
                return _DispatchDecision(
                    phase_one_target_volume=final_target_volume,
                    run_phase_two=False,
                    is_noop=False,
                )
            if abs(final_target_volume) > abs(current_volume):
                return _DispatchDecision(phase_one_target_volume=None, run_phase_two=True, is_noop=False)
            return _DispatchDecision(phase_one_target_volume=None, run_phase_two=False, is_noop=True)

        return _DispatchDecision(phase_one_target_volume=0, run_phase_two=True, is_noop=False)

    def _build_dispatch_phases(self, planning: _DispatchPlanningResult) -> _DispatchPhases:
        """将按品种计划拆分为第一阶段、第二阶段与直接结果。"""
        phase_one_tasks: list[_PreparedSymbolAlgorithm] = []
        phase_two_plans: list[_PlannedSymbolAlgorithm] = []
        direct_results: list[AlgorithmResult] = []

        for plan in planning.plans:
            decision = self._classify_symbol_dispatch_phases(plan)
            if decision.is_noop:
                direct_results.append(
                    self._build_noop_algorithm_result(
                        plan,
                        account_assets=planning.account_assets,
                        market_data=planning.market_data,
                    )
                )
                continue

            if decision.phase_one_target_volume is not None:
                phase_one_tasks.append(self._build_prepared_symbol_algorithm(plan, decision.phase_one_target_volume))
            if decision.run_phase_two:
                phase_two_plans.append(plan)

        return _DispatchPhases(
            phase_one_tasks=phase_one_tasks,
            phase_two_plans=phase_two_plans,
            direct_results=direct_results,
        )

    def _prepare_account_for_symbol_dispatch(self, dispatch_phases: _DispatchPhases) -> None:
        """在真正分发 symbol 算法前完成默认账户级撤单。"""
        if not dispatch_phases.phase_one_tasks and not dispatch_phases.phase_two_plans:
            return
        self._owner.handle_termination_checkpoint()
        self._owner.cancel_all_orders()

    def _run_phase_one_dispatch(
        self,
        phase_one_tasks: list[_PreparedSymbolAlgorithm],
    ) -> list[AlgorithmResult]:
        """执行第一阶段的减仓/平仓任务。"""
        if phase_one_tasks:
            self._owner.handle_termination_checkpoint()
        return self._run_prepared_symbol_algorithms(phase_one_tasks)

    def _run_phase_two_dispatch(
        self,
        *,
        standard_input: UnifiedStandardInput,
        planning: _DispatchPlanningResult,
        dispatch_phases: _DispatchPhases,
        phase_one_results: list[AlgorithmResult],
    ) -> list[AlgorithmResult]:
        """执行第二阶段的开仓/加仓任务。"""
        if not dispatch_phases.phase_two_plans:
            return []
        assets = planning.account_assets
        snapshot_error = None
        if dispatch_phases.phase_one_tasks:
            self._owner.handle_termination_checkpoint()
            phase_one_results[:] = self._settle_result_positions(phase_one_results)
            assert self._account_snapshot is not None
            assets, snapshot_error = self._account_snapshot
        if any(result.position_confirmed is False for result in phase_one_results):
            snapshot_error = snapshot_error or "减仓成交已确认，持仓尚待核对，已阻断后续开仓"
        if snapshot_error or self._has_unsuccessful_results(phase_one_results):
            return self._build_blocked_phase_two_results(
                dispatch_phases.phase_two_plans,
                account_assets=assets,
                market_data=planning.market_data,
                error=snapshot_error,
            )

        phase_two_results: list[AlgorithmResult] = []
        if dispatch_phases.phase_one_tasks:
            phase_two_replan = self._rebuild_phase_two_tasks(
                standard_input, dispatch_phases.phase_two_plans, account_assets=assets
            )
            phase_two_tasks = phase_two_replan.tasks
            phase_two_results.extend(phase_two_replan.planning_failures)
        else:
            phase_two_tasks = [
                self._build_prepared_symbol_algorithm(plan, plan.final_target_volume)
                for plan in dispatch_phases.phase_two_plans
            ]
        self._owner.handle_termination_checkpoint()
        if phase_two_tasks:
            # 新一阶段开始下单后，前一阶段快照不能再用于整轮收尾。
            self._account_snapshot = None
        phase_two_results.extend(self._run_prepared_symbol_algorithms(phase_two_tasks))
        return phase_two_results

    def _settle_result_positions(self, results: list[AlgorithmResult]) -> list[AlgorithmResult]:
        """一次账户查询核对所有品种，保留已确认的交易完成结论。"""
        self._account_snapshot = self._read_result_assets()
        return self._reconcile_result_positions(results, self._account_snapshot[0])

    def _reconcile_result_positions(
        self, results: list[AlgorithmResult], assets: UnifiedAccountAssets
    ) -> list[AlgorithmResult]:
        """分别保存交易结论与持仓核对事实，真实执行错误保持原样。"""
        if is_degraded_snapshot_source(assets.source):
            return results
        reconciled = []
        for result in results:
            final = self._owner.get_current_volume(result.symbol, assets)
            if not isfinite(final) or normal_session_skip(result.model_dump()):
                reconciled.append(result)
                continue
            values = {name: getattr(result, name) for name in AlgorithmResult.model_fields}
            values.update(final_volume=final, account_assets=assets)
            if result.expected_volume is not None:
                confirmed = self._is_same_volume(final, result.expected_volume)
                values.update(position_confirmed=confirmed, final_volume=final if confirmed else None)
            elif (
                result.target_volume is not None
                and (result.reason_code == TARGET_NOT_REACHED or is_success_status(result.status) and not result.error)
                and (result.reason_code == TARGET_NOT_REACHED or not self._is_same_volume(final, result.target_volume))
            ):
                values.update(
                    summarize_outcome(
                        start=result.start_volume if result.start_volume is not None else float("nan"),
                        final=final,
                        target=result.target_volume,
                        orders=result.orders,
                        trades=result.trades,
                    )
                )
            reconciled.append(AlgorithmResult(**values))
        return reconciled

    def _build_prepared_symbol_algorithm(
        self,
        plan: _PlannedSymbolAlgorithm,
        target_volume: TargetVolumeValue,
        *,
        sizing: TargetSizingDecision | None = None,
    ) -> _PreparedSymbolAlgorithm:
        """将阶段计划转换为可执行的算法任务."""
        return _PreparedSymbolAlgorithm(
            symbol=plan.symbol,
            algorithm_name=plan.algorithm_name,
            algorithm_input=AlgorithmInput(
                symbol=plan.symbol,
                target_volume=target_volume,
                trade_rule=dict(plan.trade_rule),
                params=plan.params,
            ),
            audit_context=plan.audit_context,
            sizing=sizing or plan.sizing,
        )

    def _build_noop_algorithm_result(
        self,
        plan: _PlannedSymbolAlgorithm,
        *,
        account_assets: UnifiedAccountAssets,
        market_data: dict[str, UnifiedPriceData],
    ) -> AlgorithmResult:
        """为无需执行的品种构造 NOOP 结果."""
        return AlgorithmResult(
            orders=[],
            account_assets=account_assets,
            target_volume=plan.final_target_volume,
            sizing=plan.sizing,
            first_tick=clone_price_data(market_data.get(plan.symbol)),
            memory={},
            final_volume=float(plan.final_target_volume),
            status=ExecutionStatus.NOOP,
            error=None,
            symbol=plan.symbol,
            algorithm=plan.algorithm_name,
        )

    def _build_blocked_phase_two_results(
        self,
        plans: list[_PlannedSymbolAlgorithm],
        *,
        account_assets: UnifiedAccountAssets,
        market_data: dict[str, UnifiedPriceData],
        error: str | None = None,
    ) -> list[AlgorithmResult]:
        """第一阶段失败时，为第二阶段待执行品种构造阻断结果."""
        blocked_error = error or "第一阶段存在未成功的 symbol，已跳过后续开仓阶段"
        blocked_results: list[AlgorithmResult] = []
        for plan in plans:
            blocked_results.append(
                self._build_failed_algorithm_result(
                    symbol=plan.symbol,
                    algorithm_name=plan.algorithm_name,
                    error=blocked_error,
                    status=ExecutionStatus.BLOCKED,
                    account_assets=account_assets,
                    target_volume=plan.final_target_volume,
                    sizing=plan.sizing,
                    first_tick=clone_price_data(market_data.get(plan.symbol)),
                )
            )
        return blocked_results

    def _rebuild_phase_two_tasks(
        self,
        standard_input: UnifiedStandardInput,
        plans: list[_PlannedSymbolAlgorithm],
        *,
        account_assets: UnifiedAccountAssets,
    ) -> _PhaseTwoReplanResult:
        """在第一阶段完成后，基于最新账户快照重算第二阶段目标."""
        if not plans:
            return _PhaseTwoReplanResult(tasks=[], planning_failures=[])

        phase_two_symbols = [plan.symbol for plan in plans]
        phase_two_plans = {plan.symbol: plan for plan in plans}
        refreshed_account_assets = account_assets
        effective_curr_target = self._build_effective_curr_target(standard_input, refreshed_account_assets)
        planning_market_data, sizing_decisions = self._plan_target_volumes_for_symbols(
            standard_input=standard_input,
            account_assets=refreshed_account_assets,
            symbols=phase_two_symbols,
            effective_curr_target=effective_curr_target,
        )

        tasks: list[_PreparedSymbolAlgorithm] = []
        planning_failures: list[AlgorithmResult] = []
        for symbol in phase_two_symbols:
            plan = phase_two_plans[symbol]
            sizing = sizing_decisions.get(symbol)
            target_volume = sizing.target_quantity if sizing is not None else None
            if sizing is None or target_volume is None:
                planning_failures.append(
                    self._build_failed_algorithm_result(
                        symbol=symbol,
                        algorithm_name=plan.algorithm_name,
                        error=self._build_target_volume_planning_error(
                            symbol,
                            effective_curr_target,
                            planning_market_data,
                        ),
                        account_assets=refreshed_account_assets,
                        target_volume=plan.final_target_volume,
                        first_tick=clone_price_data(planning_market_data.get(symbol)),
                        sizing=sizing,
                        status=(
                            ExecutionStatus.BLOCKED
                            if sizing is not None
                            and (
                                sizing.reason_code == "COMMON.MARKET_DATA.UNAVAILABLE"
                                or sizing.sizing_mode == "quantity"
                            )
                            else ExecutionStatus.FAILED
                        ),
                        reason_code=sizing.reason_code if sizing is not None else None,
                        reason_family=(
                            ExecutionReasonFamily.MARKET_RULE
                            if sizing is not None
                            and (
                                sizing.reason_code == "COMMON.MARKET_DATA.UNAVAILABLE"
                                or sizing.sizing_mode == "quantity"
                            )
                            else None
                        ),
                    )
                )
                continue
            if sizing is not None:
                sizing.current_quantity = self._owner.get_current_volume(symbol, refreshed_account_assets)
            tasks.append(self._build_prepared_symbol_algorithm(plan, target_volume, sizing=sizing))
        return _PhaseTwoReplanResult(tasks=tasks, planning_failures=planning_failures)

    def _has_unsuccessful_results(self, results: list[AlgorithmResult]) -> bool:
        """判断结果列表中是否存在未成功状态."""
        return any(not is_success_status(result.status) for result in results)

    def _merge_symbol_algorithm_results(self, results: list[AlgorithmResult]) -> list[AlgorithmResult]:
        """将同一品种的多阶段结果合并为单个 `AlgorithmResult`."""
        merged_results: dict[str, AlgorithmResult] = {}
        for result in results:
            if not result.symbol:
                raise ValueError("AlgorithmResult 必须包含 symbol")
            if not result.algorithm:
                raise ValueError("AlgorithmResult 必须包含 algorithm")

            existing = merged_results.get(result.symbol)
            if existing is None:
                merged_results[result.symbol] = result
                continue

            merged_results[result.symbol] = self._merge_algorithm_result(existing, result)

        return list(merged_results.values())

    def _merge_algorithm_result(
        self,
        previous: AlgorithmResult,
        current: AlgorithmResult,
    ) -> AlgorithmResult:
        """合并同一品种的前后阶段结果."""
        merged_memory = dict(previous.memory)
        merged_memory.update(current.memory)
        merged_status = self._derive_symbol_result_status([previous, current])
        merged_error = self._derive_symbol_result_error([previous, current], merged_status)
        reason_result = current if current.reason_code or current.reason_family else previous

        return AlgorithmResult(
            orders=[*previous.orders, *current.orders],
            trades=[*previous.trades, *current.trades],
            account_assets=current.account_assets,
            target_volume=current.target_volume if current.target_volume is not None else previous.target_volume,
            sizing=current.sizing if current.sizing is not None else previous.sizing,
            start_volume=previous.start_volume,
            expected_volume=current.expected_volume
            if current.expected_volume is not None
            else previous.expected_volume,
            position_confirmed=(
                current.position_confirmed if current.expected_volume is not None else previous.position_confirmed
            ),
            first_tick=current.first_tick if current.first_tick is not None else previous.first_tick,
            memory=merged_memory,
            final_volume=(
                previous.final_volume
                if current.final_volume is None
                and current.reason_code == "COMMON.MARKET_DATA.UNAVAILABLE"
                and not current.orders
                and not current.trades
                else current.final_volume
            ),
            status=merged_status,
            error=merged_error,
            reason_code=reason_result.reason_code,
            reason_family=reason_result.reason_family,
            symbol=previous.symbol,
            algorithm=current.algorithm or previous.algorithm,
        )

    def _derive_symbol_result_status(self, results: list[AlgorithmResult]) -> ExecutionStatus:
        """根据同一品种的多阶段结果推导最终状态."""
        statuses = [result.status for result in results]
        if any(status == ExecutionStatus.FAILED for status in statuses):
            if any(is_success_status(status) for status in statuses):
                return ExecutionStatus.PARTIAL
            return ExecutionStatus.FAILED
        if any(status == ExecutionStatus.PARTIAL for status in statuses):
            return ExecutionStatus.PARTIAL
        if any(status == ExecutionStatus.BLOCKED for status in statuses):
            if any(is_success_status(status) for status in statuses):
                return ExecutionStatus.PARTIAL
            return ExecutionStatus.BLOCKED
        if statuses and all(status == ExecutionStatus.NOOP for status in statuses):
            return ExecutionStatus.NOOP
        return ExecutionStatus.SUCCEEDED

    def _derive_symbol_result_error(
        self,
        results: list[AlgorithmResult],
        status: ExecutionStatus,
    ) -> str | None:
        """根据合并后的品种状态提取错误信息."""
        if is_success_status(status):
            return None

        for candidate_status in (ExecutionStatus.FAILED, ExecutionStatus.PARTIAL, ExecutionStatus.BLOCKED):
            for result in results:
                if result.status == candidate_status and result.error:
                    return result.error
        return next((result.error for result in results if result.error), None)

    def _is_zero_volume(self, value: int | float, *, tolerance: float = 1e-9) -> bool:
        """判断持仓数量是否可视为 0."""
        return abs(float(value)) <= tolerance

    def _is_same_volume(self, left: int | float, right: int | float, *, tolerance: float = 1e-9) -> bool:
        """判断两个持仓数量是否在容忍误差内相等."""
        return abs(float(left) - float(right)) <= tolerance

    def _is_same_direction(self, left: int | float, right: int | float, *, tolerance: float = 1e-9) -> bool:
        """判断两个持仓数量是否同向且非 0."""
        if self._is_zero_volume(left, tolerance=tolerance) or self._is_zero_volume(right, tolerance=tolerance):
            return False
        return (left > tolerance and right > tolerance) or (left < -tolerance and right < -tolerance)

    def _build_target_volume_planning_error(
        self,
        symbol: str,
        effective_curr_target: dict[str, float],
        planning_market_data: dict[str, UnifiedPriceData],
    ) -> str:
        """为 target volume 规划失败生成明确错误信息."""
        weight = effective_curr_target.get(symbol, 0.0)
        if symbol not in planning_market_data:
            return f"{symbol} 缺少行情数据，无法规划 target_volume"
        quote_error = self._owner.market_data_error(planning_market_data[symbol], purpose="sizing")
        if quote_error:
            return f"{symbol} 行情不可用于定量：{quote_error}"
        if weight != 0 and planning_market_data[symbol].last_price <= 0:
            return f"{symbol} 价格无效，无法规划 target_volume"
        return f"{symbol} 无法规划 target_volume"

    def _build_symbol_audit_context(
        self,
        standard_input: UnifiedStandardInput,
        symbol: str,
        algorithm_name: str,
    ) -> AuditContext | None:
        """构造单品种执行会话的审计上下文."""
        audit = standard_input.extra.get("audit")
        if not isinstance(audit, dict):
            return None

        symbol_audit = _coerce_object_dict(audit)
        symbol_audit["symbol"] = symbol
        symbol_audit["algorithm"] = algorithm_name
        return symbol_audit

    def _emit_symbol_decision_events(
        self,
        standard_input: UnifiedStandardInput,
        results: list[AlgorithmResult],
    ) -> None:
        """
        为每个品种补发 symbol 级审计事件.

        Parameters
        ----------
        standard_input : UnifiedStandardInput
            本次执行的统一输入，用于构造每个品种的审计上下文。
        results : list[AlgorithmResult]
            合并后的按品种执行结果。

        Notes
        -----
        无操作品种记为 ``SYMBOL_SKIPPED``，其余记为 ``SYMBOL_DECISION_MADE``，
        并把品种级错误写入 ``details.error``，使失败原因能进入事件流被
        前端消费，而不再只躺在执行记录里。未配置审计时 ``emit_audit_event``
        自身静默跳过。
        """
        for result in results:
            if not result.symbol:
                continue
            is_skipped = result.status == ExecutionStatus.NOOP
            event_type = ExecutionEventType.SYMBOL_SKIPPED if is_skipped else ExecutionEventType.SYMBOL_DECISION_MADE
            reason_code = result.reason_code or (
                result.sizing.reason_code
                if is_skipped and result.sizing is not None
                else ("COMMON.SYMBOL_SKIPPED" if is_skipped else "COMMON.SYMBOL_DECISION_MADE")
            )
            reason_family = result.reason_family or _SYMBOL_EVENT_REASON_FAMILY_MAP.get(
                result.status, ExecutionReasonFamily.SYSTEM
            )
            details: dict[str, object] = {
                "decision": {
                    "symbol": result.symbol,
                    "algorithm": result.algorithm,
                    "status": result.status.value,
                    "target_volume": result.target_volume,
                    "orders_count": len(result.orders),
                },
            }
            if result.sizing is not None:
                details["sizing"] = result.sizing.model_dump(mode="json")
            if result.error:
                details["error"] = result.error
            self._runtime.emit_audit_event(
                event_type=event_type,
                status=_SYMBOL_EVENT_STATUS_MAP.get(result.status, ExecutionEventStatus.INFO),
                reason_family=reason_family,
                reason_code=reason_code,
                symbol=result.symbol,
                details=details,
                audit_context=self._build_symbol_audit_context(standard_input, result.symbol, result.algorithm),
            )

    def _build_failed_algorithm_result(
        self,
        symbol: str,
        algorithm_name: str,
        error: str | None,
        *,
        outcome_reason: str | None = None,
        status: ExecutionStatus = ExecutionStatus.FAILED,
        orders: list[UnifiedOrder] | None = None,
        account_assets: UnifiedAccountAssets | None = None,
        target_volume: TargetVolumeValue | None = None,
        first_tick: UnifiedPriceData | None = None,
        memory: dict[str, object] | None = None,
        sizing: TargetSizingDecision | None = None,
        reason_code: str | None = None,
        reason_family: ExecutionReasonFamily | None = None,
    ) -> AlgorithmResult:
        """构造失败或明确阻断结果。"""
        return AlgorithmResult(
            orders=list(orders or []),
            account_assets=account_assets or self._read_result_assets()[0],
            target_volume=target_volume,
            sizing=sizing,
            first_tick=first_tick,
            memory=dict(memory or {}),
            status=status,
            error=error,
            outcome_reason=outcome_reason or error,
            reason_code=reason_code,
            reason_family=reason_family,
            symbol=symbol,
            algorithm=algorithm_name,
        )

    def _derive_dispatch_error(
        self,
        status: ExecutionStatus,
        symbol_results: dict[str, AlgorithmResult],
    ) -> str | None:
        """根据整体状态提取通用顶层错误信息。"""
        return _derive_dispatch_error(status, symbol_results)

    def _build_symbol_results(
        self,
        results: list[AlgorithmResult],
    ) -> dict[str, AlgorithmResult]:
        """根据算法结果列表构造按品种结果."""
        symbol_results: dict[str, AlgorithmResult] = {}
        for result in results:
            if not result.symbol:
                raise ValueError("AlgorithmResult 必须包含 symbol")
            if not result.algorithm:
                raise ValueError("AlgorithmResult 必须包含 algorithm")
            if result.symbol in symbol_results:
                raise ValueError(f"重复的 symbol 结果: {result.symbol}")

            symbol_results[result.symbol] = result

        return symbol_results

    def _create_standard_output_from_results(
        self,
        standard_input: UnifiedStandardInput,
        results: list[AlgorithmResult],
    ) -> UnifiedStandardOutput:
        """直接根据 symbol 级 `AlgorithmResult` 列表构造统一输出."""
        symbol_results = self._build_symbol_results(results)
        status = _derive_dispatch_status(symbol_results)
        error = self._derive_dispatch_error(status, symbol_results)
        account_assets, explicit_error = (
            self._account_snapshot if self._account_snapshot is not None else self._read_result_assets()
        )
        only_normal_skips = bool(results) and all(normal_session_skip(result.model_dump()) for result in results)
        if (
            explicit_error
            and not only_normal_skips
            and not all(result.expected_volume is not None and result.success for result in results)
            and status
            in {
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.NOOP,
                ExecutionStatus.PARTIAL,
            }
        ):
            # 只有依赖持仓事实的结论才因收尾查询失败而降级。
            # BLOCKED/FAILED 且未下单并不声称仓位，不能改写成「最终持仓查询失败」。
            progressed = any(
                result.orders or result.trades or result.status in {ExecutionStatus.PARTIAL, ExecutionStatus.SUCCEEDED}
                for result in results
            )
            status = ExecutionStatus.PARTIAL if progressed else ExecutionStatus.FAILED
            error = error or explicit_error

        return UnifiedStandardOutput(
            account_assets=account_assets,
            memory=self._runtime.memory,
            symbol_results=symbol_results,
            status=status,
            error=error,
            reason_code=self._session_reason_code(status, symbol_results, explicit_error),
            execution_time=self._runtime.elapsed_seconds(),
            channel_type=self._owner.channel_type,
            inputs=standard_input,
        )

    @staticmethod
    def _session_reason_code(
        status: ExecutionStatus, symbol_results: dict[str, AlgorithmResult], explicit_error: str | None
    ) -> str | None:
        """部分休市仅在没有收尾查询错误时提升原因码，保留真实异常。"""
        if status == ExecutionStatus.PARTIAL and not explicit_error and _only_session_closed_failures(symbol_results):
            return COMMON_SESSION_CLOSED
        if is_success_status(status) and any(
            normal_session_skip(result.model_dump()) for result in symbol_results.values()
        ):
            return COMMON_SESSION_CLOSED
        return None

    def _read_result_assets(self) -> tuple[UnifiedAccountAssets, str | None]:
        """收尾查询失败也保留已生成的品种与订单证据。"""
        try:
            assets = self._owner.get_account_assets()
            if any(not isfinite(position.volume) for position in assets.positions):
                return UnifiedAccountAssets.unavailable(), "最终持仓尚未确认"
            if is_degraded_snapshot_source(assets.source):
                return assets, "最终持仓尚未确认"
            return assets, None
        except (ExecutionTerminated, MemoryError):
            raise
        except Exception as exc:  # noqa: BLE001 - 收尾必须保留渠道 SDK 查询失败前的执行证据
            if getattr(exc, "requires_session_recovery", False):
                raise
            self._owner.logger.exception("执行收尾持仓查询失败")
            return UnifiedAccountAssets.unavailable(), execution_error_message(exc, "最终持仓查询")
