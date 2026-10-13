"""`AbstractExecutor` 的默认能力实现.

本文件放置渠道执行器可复用、可重写的通用能力，
例如输入校验、目标仓位计算、持仓读取与符号集合推导。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

from axile.channels import get_channel
from axile.common.order_param_model import OrderParamModel
from axile.executor.models.execution_result import (
    ExecutionOutcome,
    ExecutionStatus,
    TargetSizingDecision,
    TargetSizingStatus,
)
from axile.executor.models.unified_account_assets import Position, PositionDirection, UnifiedAccountAssets
from axile.executor.models.unified_input import UnifiedStandardInput
from axile.executor.models.unified_output import UnifiedStandardOutput
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.models.value_conversion import PriceValueConversion
from axile.executor.session_closed import COMMON_SESSION_CLOSED, SESSION_CLOSED_MESSAGE

if TYPE_CHECKING:
    from axile.executor.abstract_executor.base import AbstractExecutor


def _executor(owner: object) -> AbstractExecutor:
    return cast("AbstractExecutor", owner)


def _available_cpu_count() -> int:
    """返回当前进程可用的 CPU 数，并兼容不支持 affinity 的平台."""
    try:
        affinity = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        affinity = None
    if affinity:
        return len(affinity)
    return max(1, os.cpu_count() or 1)


def _system_max_parallel_symbol_workers() -> int:
    """根据执行环境推导单次调度的 symbol worker 保护上限."""
    return min(32, max(8, _available_cpu_count() * 4))


class AbstractExecutorCapabilityMixin:
    """承载渠道可复用、可重写的默认能力实现."""

    def get_all_symbols(
        self,
        curr_target: dict[str, float] | None = None,
        last_target: dict[str, float] | None = None,
    ) -> list[str]:
        """
        获取当前执行涉及的全部品种列表.

        Parameters
        ----------
        curr_target : dict[str, float] | None, default=None
            当前目标仓位映射。
        last_target : dict[str, float] | None, default=None
            上一次目标仓位映射。

        Returns
        -------
        list[str]
            当前执行涉及的全部品种代码列表。

        Raises
        ------
        ValueError
            当 ``curr_target`` 或 ``last_target`` 未提供时抛出。
        """
        if curr_target is None or last_target is None:
            raise ValueError("curr_target和last_target参数必须提供")
        symbols = list(curr_target.keys())
        for symbol in last_target:
            if symbol not in curr_target:
                symbols.append(symbol)
        return symbols

    def _create_non_trading_output(self, standard_input: UnifiedStandardInput) -> UnifiedStandardOutput:
        """
        创建非交易时间场景下的阻断输出.

        Parameters
        ----------
        standard_input : UnifiedStandardInput
            当前执行使用的统一输入对象。

        Returns
        -------
        UnifiedStandardOutput
            状态为 ``BLOCKED`` 的统一输出对象。
        """
        executor = _executor(self)
        runtime = executor.require_execution_runtime()
        empty_assets = UnifiedAccountAssets.unavailable()

        return UnifiedStandardOutput(
            account_assets=empty_assets,
            memory={"message": SESSION_CLOSED_MESSAGE},
            symbol_results={},
            outcome=ExecutionOutcome.BLOCKED,
            outcome_reason=SESSION_CLOSED_MESSAGE,
            status=ExecutionStatus.BLOCKED,
            error=SESSION_CLOSED_MESSAGE,
            reason_code=COMMON_SESSION_CLOSED,
            execution_time=runtime.elapsed_seconds(),
            channel_type=executor.channel_type,
            inputs=standard_input,
        )

    def _supports_parallel_symbol_dispatch(self) -> bool:
        """
        返回当前执行器是否支持安全的按品种并行调度.

        Returns
        -------
        bool
            支持安全并行调度时返回 ``True``。
        """
        return self._max_parallel_symbol_workers() > 1

    def _max_parallel_symbol_workers(self) -> int:
        """
        返回系统保护与渠道能力共同允许的最大品种并发数.

        Returns
        -------
        int
            环境保护上限与可选渠道上限的较小值；渠道未注册时安全回退为 ``1``。
        """
        executor = _executor(self)
        try:
            channel_limit = get_channel(executor.channel_type).max_parallel_symbols
        except KeyError:
            return 1
        system_limit = _system_max_parallel_symbol_workers()
        return system_limit if channel_limit is None else min(system_limit, channel_limit)

    @property
    def order_param_model(self) -> OrderParamModel:
        """
        返回当前渠道的订单参数模型.

        Returns
        -------
        OrderParamModel
            渠道插件声明的模型；渠道未注册或插件未声明时安全回退为
            ``UNKNOWN``,由算法层按"歧义即失败"处理。
        """
        executor = _executor(self)
        try:
            return get_channel(executor.channel_type).order_param_model
        except KeyError:
            return OrderParamModel.UNKNOWN

    def _normalize_symbol(self, symbol: str) -> str:
        """返回当前渠道在统一层使用的规范 symbol."""
        return symbol

    def _normalize_standard_input(self, standard_input: UnifiedStandardInput) -> UnifiedStandardInput:
        """返回当前渠道在 planning 前使用的标准输入."""
        return standard_input

    def _normalize_connected_standard_input(self, standard_input: UnifiedStandardInput) -> UnifiedStandardInput:
        """在渠道连接及其运行时目录就绪后规范化标准输入."""
        return standard_input

    def _prepare_instrument_catalog(self, standard_input: UnifiedStandardInput) -> None:
        """渠道可在 planning 前准备本次所需合约资料。"""

    def _release_instrument_catalog(self) -> None:
        """渠道可在执行结束时解除固定资料副本。"""

    def _validate_input(self, standard_input: UnifiedStandardInput) -> None:
        """
        执行通用输入验证.

        Parameters
        ----------
        standard_input : UnifiedStandardInput
            待校验的统一输入对象。

        Raises
        ------
        ValueError
            当账户配置缺失时抛出。
        """
        executor = _executor(self)

        # 空字典是合法空仓目标，已有持仓由 planning 补为零权重。
        if not standard_input.account_config:
            raise ValueError("账户配置不能为空")

        executor.logger.info(f"输入参数验证通过 - 目标持仓: {standard_input.curr_target}")

    def _calculate_target_volume_base(
        self,
        curr_target: dict[str, float],
        account_assets: UnifiedAccountAssets,
        market_data: dict[str, UnifiedPriceData],
        trade_rules: dict[str, dict[str, object]],
        last_target: dict[str, float],
        forbidden_symbols: list[str] | None = None,
    ) -> dict[str, int | float]:
        """
        执行通用目标持仓数量计算.

        Parameters
        ----------
        curr_target : dict[str, float]
            当前目标仓位权重映射。
        account_assets : UnifiedAccountAssets
            当前账户资产快照。
        market_data : dict[str, UnifiedPriceData]
            品种到最新行情快照的映射。
        trade_rules : dict[str, dict[str, object]]
            各品种交易规则映射。
        last_target : dict[str, float]
            上次目标仓位权重映射。
        forbidden_symbols : list[str] | None, default=None
            禁止交易的品种列表。

        Returns
        -------
        dict[str, int | float]
            各品种计算后的目标持仓数量。
        """
        decisions = AbstractExecutorCapabilityMixin._calculate_target_sizing_base(
            self,
            curr_target,
            account_assets,
            market_data,
            trade_rules,
            last_target,
            forbidden_symbols,
        )
        return {
            symbol: decision.target_quantity
            for symbol, decision in decisions.items()
            if decision.target_quantity is not None
        }

    def _calculate_target_sizing_base(
        self,
        curr_target: dict[str, float],
        account_assets: UnifiedAccountAssets,
        market_data: dict[str, UnifiedPriceData],
        trade_rules: dict[str, dict[str, object]],
        last_target: dict[str, float],
        forbidden_symbols: list[str] | None = None,
    ) -> dict[str, TargetSizingDecision]:
        """按统一目标集合生成逐品种数量及其换算证据."""
        executor = _executor(self)
        forbidden_symbol_set = set(forbidden_symbols or [])
        working_target = dict(curr_target)

        for symbol in last_target:
            if symbol in forbidden_symbol_set:
                continue
            if symbol not in working_target:
                working_target[symbol] = 0.0

        decisions: dict[str, TargetSizingDecision] = {}

        for symbol, weight in working_target.items():
            if weight == 0:
                decisions[symbol] = TargetSizingDecision(
                    symbol=symbol,
                    reason_code="COMMON.SIZING.ZERO_TARGET",
                    account_weight=weight,
                    equity=account_assets.total_asset,
                    raw_quantity=0.0,
                    target_quantity=0.0,
                )
                continue

            if symbol not in market_data:
                executor.logger.warning(f"品种 {symbol} 没有价格数据，跳过")
                decisions[symbol] = TargetSizingDecision(
                    symbol=symbol,
                    status=TargetSizingStatus.UNAVAILABLE,
                    reason_code="COMMON.SIZING.MISSING_MARKET_DATA",
                    account_weight=weight,
                    equity=account_assets.total_asset,
                )
                continue

            price = market_data[symbol].last_price
            if price <= 0:
                executor.logger.warning(f"品种 {symbol} 价格无效: {price}")
                decisions[symbol] = TargetSizingDecision(
                    symbol=symbol,
                    status=TargetSizingStatus.UNAVAILABLE,
                    reason_code="COMMON.SIZING.INVALID_PRICE",
                    account_weight=weight,
                    equity=account_assets.total_asset,
                    reference_price=price,
                )
                continue

            trade_rule = trade_rules.get(symbol, {})
            instance_volume = getattr(executor, "__dict__", {}).get("_calculate_generic_volume")
            class_volume = getattr(type(executor), "_calculate_generic_volume", None)
            if callable(instance_volume) or (
                class_volume is not None
                and class_volume is not AbstractExecutorCapabilityMixin._calculate_generic_volume
            ):
                volume = executor._calculate_generic_volume(
                    weight,
                    price,
                    account_assets,
                    trade_rule,
                    symbol=symbol,
                )
                raw_quantity = account_assets.total_asset * weight / price
                decisions[symbol] = TargetSizingDecision(
                    symbol=symbol,
                    reason_code=(
                        "COMMON.SIZING.EXACT"
                        if abs(raw_quantity - float(volume)) <= 1e-12
                        else "COMMON.SIZING.QUANTIZED"
                    ),
                    account_weight=weight,
                    equity=account_assets.total_asset,
                    reference_price=price,
                    unit_multiplier=1.0,
                    unit_notional=price,
                    target_notional=abs(account_assets.total_asset * weight),
                    raw_quantity=raw_quantity,
                    target_quantity=float(volume),
                )
                continue
            decisions[symbol] = executor._calculate_generic_sizing(
                weight,
                price,
                account_assets,
                trade_rule,
                symbol=symbol,
            )
        return decisions

    calculate_target_volume_base = _calculate_target_volume_base
    calculate_target_sizing_base = _calculate_target_sizing_base

    def _calculate_generic_sizing(
        self,
        weight: float,
        price: float,
        account_assets: UnifiedAccountAssets,
        trade_rule: dict[str, object],
        *,
        symbol: str | None = None,
    ) -> TargetSizingDecision:
        """生成通用现货数量及其取整证据."""
        executor = _executor(self)
        min_volume_raw = trade_rule.get("最小交易单位", 1)
        min_volume = (
            float(min_volume_raw)
            if isinstance(min_volume_raw, int | float) and not isinstance(min_volume_raw, bool)
            else 1.0
        )
        precision_raw = trade_rule.get("quantity_precision")
        precision = precision_raw if isinstance(precision_raw, int) and not isinstance(precision_raw, bool) else None
        raw_quantity = account_assets.total_asset * weight / price
        target_quantity = raw_quantity
        quantity_step: float | None = None
        if precision is not None:
            quantity_step = 10 ** (-precision)
            target_quantity = round(raw_quantity, precision)
        elif min_volume > 1:
            quantity_step = min_volume
            target_quantity = int(raw_quantity / min_volume) * min_volume

        min_notional: float | None = None
        if symbol:
            try:
                min_notional = executor.get_min_notional(symbol)
            except Exception:
                min_notional = None
        reason_code = "COMMON.SIZING.EXACT"
        if raw_quantity != 0 and target_quantity == 0:
            reason_code = "COMMON.SIZING.BELOW_MIN_QUANTITY"
        elif abs(raw_quantity - target_quantity) > 1e-12:
            reason_code = "COMMON.SIZING.QUANTIZED"

        return TargetSizingDecision(
            symbol=symbol or "",
            account_weight=weight,
            equity=account_assets.total_asset,
            reference_price=price,
            unit_multiplier=1.0,
            unit_notional=price,
            target_notional=abs(account_assets.total_asset * weight),
            raw_quantity=raw_quantity,
            target_quantity=float(target_quantity),
            quantity_step=quantity_step,
            min_quantity=quantity_step,
            min_notional=min_notional,
            reason_code=reason_code,
        )

    def _calculate_generic_volume(
        self,
        weight: float,
        price: float,
        account_assets: UnifiedAccountAssets,
        trade_rule: dict[str, object],
        *,
        symbol: str | None = None,
    ) -> float:
        """
        执行通用持仓数量计算.

        Parameters
        ----------
        weight : float
            目标仓位权重。
        price : float
            当前价格。
        account_assets : UnifiedAccountAssets
            当前账户资产快照。
        trade_rule : dict[str, object]
            品种交易规则；渠道实现可通过 ``contract_multiplier`` 等键
            注入合约乘数，避免在路由元数据中夹带业务字段。
        symbol : str | None, optional
            正在计算目标手数的品种代码，由调度层透传。
            通用基类不依赖 symbol，渠道 override 可用它读取合约缓存
            （例如 CTP 的 ``InstrumentField.VolumeMultiple``）。

        Returns
        -------
        float
            计算得到的目标持仓数量。

        Notes
        -----
        渠道 override 中的目标手数语义可通过 ``trade_rule['sizing_mode']``
        切换：``weight``（默认）/ ``lots``（直接当目标手数）/
        ``premium_weight``（按权利金权重换算，期权专用）。
        """
        decision = self._calculate_generic_sizing(
            weight,
            price,
            account_assets,
            trade_rule,
            symbol=symbol,
        )
        return float(decision.target_quantity or 0.0)

    def get_current_volume(self, symbol: str, account_assets: UnifiedAccountAssets) -> float:
        """
        获取指定品种的当前净持仓数量.

        Parameters
        ----------
        symbol : str
            品种代码。
        account_assets : UnifiedAccountAssets
            当前账户资产快照。

        Returns
        -------
        float
            将多头与空头方向合并后的净持仓数量。
        """
        net_volume = 0.0
        for position in account_assets.positions:
            if position.symbol == symbol:
                if position.direction == PositionDirection.LONG:
                    net_volume += position.volume
                elif position.direction == PositionDirection.SHORT:
                    net_volume -= position.volume
        return net_volume

    def get_positions_for_symbol(
        self, symbol: str, account_assets: UnifiedAccountAssets
    ) -> list[tuple[float, PositionDirection]]:
        """
        获取指定品种的所有方向持仓.

        Parameters
        ----------
        symbol : str
            品种代码。
        account_assets : UnifiedAccountAssets
            当前账户资产快照。

        Returns
        -------
        list[tuple[float, PositionDirection]]
            该品种各方向持仓数量与方向的列表。
        """
        result: list[tuple[float, PositionDirection]] = []
        for position in account_assets.positions:
            if position.symbol == symbol and position.volume > 0:
                result.append((position.volume, position.direction))
        return result

    def calculate_target_volume(
        self,
        curr_target: dict[str, float],
        account_assets: UnifiedAccountAssets,
        market_data: dict[str, UnifiedPriceData],
        trade_rules: dict[str, dict[str, object]],
        last_target: dict[str, float],
        forbidden_symbols: list[str] | None = None,
    ) -> dict[str, int | float]:
        """
        执行默认的目标仓位数量计算逻辑.

        Parameters
        ----------
        curr_target : dict[str, float]
            当前目标仓位权重映射。
        account_assets : UnifiedAccountAssets
            当前账户资产快照。
        market_data : dict[str, UnifiedPriceData]
            品种到最新行情快照的映射。
        trade_rules : dict[str, dict[str, object]]
            各品种交易规则映射。
        last_target : dict[str, float]
            上次目标仓位权重映射。
        forbidden_symbols : list[str] | None, default=None
            禁止交易的品种列表。

        Returns
        -------
        dict[str, int | float]
            各品种计算后的目标持仓数量。
        """
        executor = _executor(self)
        return executor._calculate_target_volume_base(
            curr_target,
            account_assets,
            market_data,
            trade_rules,
            last_target,
            forbidden_symbols,
        )

    def calculate_target_sizing(
        self,
        curr_target: dict[str, float],
        account_assets: UnifiedAccountAssets,
        market_data: dict[str, UnifiedPriceData],
        trade_rules: dict[str, dict[str, object]],
        last_target: dict[str, float],
        forbidden_symbols: list[str] | None = None,
    ) -> dict[str, TargetSizingDecision]:
        """返回目标数量与换算证据；供执行规划与审计共用."""
        executor = _executor(self)
        decisions = executor._calculate_target_sizing_base(
            curr_target,
            account_assets,
            market_data,
            trade_rules,
            last_target,
            forbidden_symbols,
        )
        for symbol, decision in decisions.items():
            if decision.status == TargetSizingStatus.SIZED:
                decision.value_conversion = executor._get_execution_value_conversion(symbol, account_assets)
        return decisions

    def _get_execution_value_conversion(
        self, symbol: str, account_assets: UnifiedAccountAssets
    ) -> PriceValueConversion | None:
        """返回规划快照的金额换算依据；默认渠道保持原有原生金额口径。"""
        return None

    def _calculate_last_target_unified(self, positions: list[Position]) -> dict[str, float]:
        """
        基于当前持仓市值占比计算统一 ``last_target``.

        Parameters
        ----------
        positions : list[Position]
            当前持仓列表。

        Returns
        -------
        dict[str, float]
            各品种的历史目标权重映射。
        """
        empty_result: dict[str, float] = {}
        if not positions:
            return empty_result

        if any(pos.market_value is None for pos in positions):
            return empty_result
        total_asset = sum(pos.market_value for pos in positions if pos.market_value is not None)
        if total_asset <= 0:
            return empty_result

        last_target: dict[str, float] = {}
        for pos in positions:
            if pos.volume > 0 and pos.market_value is not None and pos.market_value > 0:
                weight = pos.market_value / total_asset
                if hasattr(pos, "direction") and pos.direction == PositionDirection.SHORT:
                    weight = -weight
                last_target[pos.symbol] = weight

        return last_target
