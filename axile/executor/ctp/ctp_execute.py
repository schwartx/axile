"""直接适配 OpenCTP TraderApi 与 MdApi 的 CTP 执行器。"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar, cast, override
from zoneinfo import ZoneInfo

from openctp_ctp import thostmduserapi as md
from openctp_ctp import thosttraderapi as td

from axile.channels.cn_futures import canonicalize_cn_futures_symbol, czce_is_option_instrument
from axile.common.trade_channel import TradeChannel
from axile.executor.abstract_executor.base import AbstractExecutor
from axile.executor.account_control.decorators import run_controlled_call
from axile.executor.account_control.exceptions import AccountControlBlockedError
from axile.executor.account_control.guard import AccountControlGuard
from axile.executor.account_control.quota_state import AccountControlQuotaState
from axile.executor.algorithms.utils import clock_now
from axile.executor.algorithms.utils.clock import clock_monotonic, get_default_clock
from axile.executor.catalog_execution import CatalogExecutionMixin
from axile.executor.china_futures_session import is_within_possible_china_futures_session
from axile.executor.connection_presentation import connection_problem
from axile.executor.connection_state import connection_error_scope, connection_monitor, preserve_connection_failure
from axile.executor.constants.order_status import OrderStatus
from axile.executor.ctp.combination import split_combination_position
from axile.executor.ctp.connection import report_ctp_failure, report_ctp_stage
from axile.executor.ctp.connection_details import step_details
from axile.executor.ctp.converters import (
    account_to_unified,
    order_to_unified,
    quote_to_unified,
    stable_order_id,
    trade_to_unified,
)
from axile.executor.ctp.options import (
    OptionActionRecord,
    OptionActionStatus,
    OptionActionType,
    accept_option_action,
    build_option_cancel,
    build_option_insert,
    fail_option_action,
    finish_option_action,
    option_ref,
)
from axile.executor.ctp.quote_time import resolve_quote_time
from axile.executor.ctp.quote_validation import (
    fresh_valuation_quote_error,
    price_in_bounds,
    quote_error,
    valuation_quote_error,
)
from axile.executor.ctp.requests import (
    build_authenticate,
    build_logout,
    build_market_login,
    build_order_cancel,
    build_order_insert,
    build_query_account,
    build_query_depth_market_data,
    build_query_orders,
    build_query_positions,
    build_query_settlement_confirm,
    build_query_trades,
    build_settlement_confirm,
    build_trader_login,
    resolve_offset,
)
from axile.executor.ctp.spi import MarketSpi, TraderSpi
from axile.executor.ctp_input_validation import (
    CTPInputValidationError,
    validate_cancel_id,
    validate_config,
    validate_instrument,
)
from axile.executor.ctp_product_sessions import (
    decide_ctp_product_session,
    get_ctp_product_sessions,
)
from axile.executor.ctp_query_wait import QueryIdleClock
from axile.executor.execution_engine import ExecutionEngine
from axile.executor.futures_order_intent import is_close_intent, single_close_offset
from axile.executor.instrument_catalog import catalog_deadline, catalog_remaining
from axile.executor.instrument_catalog_records import Instrument, InstrumentRecord, from_ctp
from axile.executor.models.execution_result import AlgorithmResult, ExecutionStatus, TargetSizingDecision
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.executor.models.unified_callback import (
    UnifiedCallbackClient,
)
from axile.executor.models.unified_input import AccountConfig, CTPAccountConfig, UnifiedStandardInput
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder
from axile.executor.order_input_validation import normalize_order_enums
from axile.executor.order_insert_rejects import (
    insert_error_detail,
    rejected_order_update,
    resolve_insert_reject_order_id,
)
from axile.executor.order_volume_limits import (
    effective_max_order_volume,
    ensure_order_volume_allowed,
)
from axile.executor.quote_diagnostics import QuoteDiagnostics
from axile.executor.session_closed import COMMON_SESSION_CLOSED, SESSION_CLOSED_MESSAGE
from axile.executor.termination_cleanup import cleanup_query_allowed
from axile.executor.trading_calendar import CHINA_CALENDAR_ID, ShinnyTradingCalendar

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ValueT = TypeVar("_ValueT")


class CtpRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        return_code: int | None = None,
        execution_error: str | None = None,
        error_id: int | None = None,
        broker_message: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.return_code = return_code
        self.execution_error = execution_error
        self.error_id = error_id
        self.broker_message = broker_message
        self.reason_code = reason_code


class _TradeAssociationPending(CtpRequestError):
    """同交易日成交暂缺订单映射，可由后到订单补关联。"""


class CtpSessionRecoveryRequired(CtpRequestError):
    """当前 CTP 会话失效，必须由宿主重建并重新核对。"""

    requires_session_recovery = True


@dataclass
class _PendingQuery:
    rows: list[object]
    done: threading.Event
    error: Exception | None = None
    idle: QueryIdleClock | None = None
    timeout: float | None = None


@dataclass
class _Stage:
    done: threading.Event
    error: Exception | None = None
    request_id: int | None = None


def _copy_native_row(row):
    """复制 SWIG 回调帧，解除其与 OpenCTP 复用缓冲区的绑定。"""
    values = {}
    for name in dir(row):
        if name.startswith("_") or name in {"this", "thisown"}:
            continue
        value = getattr(row, name)
        if callable(value):
            continue
        values[name] = value
    return SimpleNamespace(**values)


def _session_block_message(reason_code: str) -> str:
    """翻译非闭市的时段检查结果。闭市由 ``_symbol_session_block`` 直出公共码。"""
    return {
        "CTP.SESSION.NO_METADATA": "合约资料不可用，交易时段尚未确认",
        "CTP.SESSION.NO_SESSION_TABLE": "未配置合约交易时段",
        "CTP.SESSION.CALENDAR_UNAVAILABLE": "交易日历不可用，交易时段尚未确认",
    }.get(reason_code, "交易时段尚未确认")


class CtpExecutionEngine(ExecutionEngine):
    """CTP 的品种时段筛选与 scoped cancel 编排器。"""

    def _symbol_session_block(self, symbol: str) -> tuple[str, str] | None:
        reason_code = cast("CTPExecutor", self._owner)._get_ctp_session_block_reason(symbol)
        if reason_code is None:
            return None
        if reason_code == "CTP.SESSION.CLOSED":
            return COMMON_SESSION_CLOSED, SESSION_CLOSED_MESSAGE
        return reason_code, _session_block_message(reason_code)

    def _derive_dispatch_error(
        self,
        status: ExecutionStatus,
        symbol_results: dict[str, AlgorithmResult],
    ) -> str | None:
        return super()._derive_dispatch_error(status, symbol_results)


class CTPExecutor(CatalogExecutionMixin, AbstractExecutor, UnifiedCallbackClient):
    """直接持有 OpenCTP API 的统一执行器。"""

    _catalog_requires_specs = True

    def __init__(self, channel_type: TradeChannel, account_config: AccountConfig | None = None) -> None:
        self._init_instrument_catalog()
        self._catalog_query_state = threading.local()
        self._catalog_control_factory = None
        self._control_quota_state = AccountControlQuotaState()
        self._validation_catalog: Callable[[], Mapping[str, InstrumentRecord]] | None = None
        self._trader_api = self._market_api = None
        self._trader_spi = self._market_spi = None
        self._pending_queries = {}
        self._instruments = {}
        self._catalog_progress_at = 0.0
        self._quotes = {}
        self._valuation_quotes = {}
        self._quote_time_logged = set()
        self._quote_rejections = {}
        self._quote_diagnostics = QuoteDiagnostics()
        self._quote_calendar = ShinnyTradingCalendar()
        self._order_keys = {}
        self._option_actions = {}
        self._order_callbacks = []
        self._trade_callbacks = []
        self._price_callbacks = []
        self._lock = threading.RLock()
        self._query_lock = threading.Lock()
        self._request_id = 0
        self._order_ref = 0
        self._trading_day = ""
        self._front_id = self._session_id = 0
        self._trader_connected = self._market_connected = False
        self._closed = False
        self._logout_in_progress = False
        self._logout_pending: dict[str, _Stage] = {}
        self._logout_disconnect_reason: int | None = None
        # 一个实例只承载一轮连接；断线后由 worker 创建新实例，禁止原地复活。
        self._ready = False
        self._invalid_reason = None
        self._connection_started = False
        self.connection_records: dict[str, object] = {}
        self._catalog_queried = False
        self.settlement_confirmation: dict[str, object] | None = None
        self._subscriptions = set()
        self._subscription_acks = set()
        self._subscription_errors = {}
        self._exchange_orders = {}
        self._ambiguous_exchange_orders = set()
        self._unassociated_trades = {}
        self._dispatched_trades = set()
        self._startup_trades = []
        self._recovery_snapshot = None
        self._monitoring = False
        self._timeout = 15.0
        self._flow_dir = None
        self._auth = _Stage(threading.Event())
        self._login = _Stage(threading.Event())
        self._settlement = _Stage(threading.Event())
        self._md_login = _Stage(threading.Event())
        super().__init__(channel_type, account_config)

    def _config(self):
        if not isinstance(self.account_config, CTPAccountConfig):
            raise RuntimeError("CTP 配置不可用")
        return self.account_config

    def _next_id(self):
        with self._lock:
            self._request_id += 1
            return self._request_id

    @staticmethod
    def _error(info, name):
        code = int(getattr(info, "ErrorID", 0) or 0) if info else 0
        if not code:
            return None
        reason = {31: "资金不足", 3: "CTP 登录校验失败"}.get(code, f"{name}失败，渠道返回错误码 {code}")
        reason_code = None
        if name == "认证" and code == 7:
            reason = "交易柜台尚未初始化，账户通道暂未就绪"
            reason_code = "CTP_NOT_INITIALIZED"
        return CtpRequestError(
            f"{name}失败: ErrorID={code}, {getattr(info, 'ErrorMsg', '')}",
            execution_error=reason,
            error_id=code,
            broker_message=str(getattr(info, "ErrorMsg", "") or ""),
            reason_code=reason_code,
        )

    @staticmethod
    def _check(code, name):
        if code != 0:
            raise CtpRequestError(
                f"{name}同步拒绝: return_code={code}",
                return_code=code,
                execution_error=f"{name}请求未受理，返回码 {code}",
            )

    def _call_trader_request(self, name, req, *, before_send=None) -> int:
        """直接发送一次 TraderApi 请求，不附加账户控制或适配层重试。"""
        if hasattr(req, "InstrumentID"):
            validate_instrument(
                req.InstrumentID,
                allow_all=name in {"ReqQryInstrument", "ReqQryOrder", "ReqQryTrade", "ReqQryInvestorPosition"},
            )
        guard = getattr(self._catalog_query_state, "guard", None) or self.get_account_control_guard()
        rid = self._next_id()
        # 账户控制可能等待很久，故门禁必须落在等待之后的原生请求发送点。
        with self._lock:
            self._require_active_connection()
            if name.startswith("ReqQry"):
                self._catalog_checkpoint()
            if name in {"ReqOrderInsert", "ReqExecOrderInsert", "ReqOptionSelfCloseInsert"}:
                self._require_new_order_ready(str(req.InstrumentID))
                if name == "ReqOrderInsert":
                    self._validate_order_quote(str(req.InstrumentID), req.LimitPrice, req.OrderPriceType)
            elif name in {"ReqOrderAction", "ReqExecOrderAction", "ReqOptionSelfCloseAction"}:
                self._require_session_ready()
            if before_send is not None:
                before_send(rid)
            self._link_control_request(guard, rid, name, req)
            code = self._invoke_trader_request(name, req, rid, guard)
        if code == 0:
            return rid
        self._control_request_result(rid, "broker_sync_rejected", {"return_code": code})
        meanings = {
            -1: "网络连接失败",
            -2: "前置未处理请求队列超限",
            -3: "每秒发送请求数超过柜台许可",
        }
        policy = getattr(guard, "policy", None)
        preset = getattr(policy, "preset_key", "unknown")
        waited_ms = guard.current_waited_ms() if guard is not None else 0
        wait_hits = guard.current_wait_hits() if guard is not None else ()
        meaning = meanings.get(code, "柜台返回未知同步拒绝")
        message = (
            f"{name}：{meaning}，返回码={code}，生效 preset={preset}，"
            f"group=ctp_td_global，rule={','.join(wait_hits) or '当前有效规则'}，"
            f"已等待={waited_ms}ms；请求未受理、未自动重试"
        )
        if code == -2:
            error = CtpSessionRecoveryRequired(message, return_code=code, execution_error=f"{meaning}，请求未受理")
            self._invalidate_connection(
                error,
                connection_failure=True,
                label="前置请求队列超限",
                presentation=lambda: self._session_problem(
                    "前置请求队列超限",
                    "柜台返回 -2，前置未处理请求队列超限；当前会话需要重建。",
                    {"原生请求": name, "返回码": code},
                ),
            )
            raise error
        error = CtpRequestError(message, return_code=code, execution_error=f"{meaning}，请求未受理")
        if code == -1:
            self._invalidate_connection(
                error,
                connection_failure=True,
                label="交易网络异常",
                presentation=lambda: self._session_problem(
                    "交易网络异常",
                    "原生交易请求返回 -1，网络连接失败；当前会话需要重建。",
                    {"原生请求": name, "返回码": code},
                ),
            )
        raise error

    def _invoke_trader_request(self, name, req, rid, guard) -> int:
        """本地校验完成后记录报单与撤单调用；其他请求不计入报撤。"""
        from axile.executor.account_control.context import controlled_source

        recorder = getattr(self, "order_submission_recorder", None)
        if name not in {"ReqOrderInsert", "ReqOrderAction"} or recorder is None:
            return int(getattr(self._trader_api, name)(req, rid))
        source = None if guard is None else controlled_source(guard, guard.source).value
        front_id = self._front_id if name == "ReqOrderInsert" else int(req.FrontID)
        session_id = self._session_id if name == "ReqOrderInsert" else int(req.SessionID)
        return recorder.submit(
            lambda: getattr(self._trader_api, name)(req, rid),
            symbol=str(req.InstrumentID),
            order_id=stable_order_id(self._trading_day, front_id, session_id, str(req.OrderRef)),
            request_id=rid,
            execution_id=getattr(guard, "execution_id", None),
            source=source,
            operation="cancel_order" if name == "ReqOrderAction" else "place_order",
        )

    def _link_control_request(self, guard, rid, name, req) -> None:
        """在发送前登记有界关联，回调可先于同步调用返回。"""
        from collections import OrderedDict

        if guard is None or not hasattr(guard, "active_attempt_indices"):
            return
        indices = guard.active_attempt_indices()
        if not indices:
            return
        links = getattr(self, "_control_request_links", None)
        if links is None:
            links = self._control_request_links = OrderedDict()
        ref = str(getattr(req, "OrderRef", ""))
        identity = stable_order_id(self._trading_day, self._front_id, self._session_id, ref)
        links[rid] = (guard, indices, name, ref, identity)
        while len(links) > 512:
            links.popitem(last=False)

    def _control_request_result(self, rid, outcome, metadata=None) -> None:
        """只更新真实关联到的旧 guard，不消费或退回额度。"""
        link = getattr(self, "_control_request_links", {}).get(rid)
        if link is not None:
            guard, indices, _, _, _ = link
            for index in indices:
                guard.update_event_outcome(index, outcome=outcome, metadata=metadata)

    def _control_order_result(self, row, outcome, metadata=None, *, order_id=None) -> None:
        recorder = getattr(self, "order_submission_recorder", None)
        if recorder is not None and order_id is not None:
            recorder.broker_result(
                order_id,
                rejected=outcome == "broker_rejected",
                error_id=(metadata or {}).get("error_id"),
            )
        ref = str(getattr(row, "OrderRef", ""))
        if not ref:
            return
        links = getattr(self, "_control_request_links", {})
        matches = [
            rid
            for rid, (_, _, name, order_ref, identity) in list(links.items())
            if name == "ReqOrderInsert" and ref == order_ref and order_id == identity
        ]
        if len(matches) == 1:
            self._control_request_result(matches[0], outcome, metadata)

    def _on_control_action_error(self, row, info, request_id=None) -> None:
        """撤单拒绝仅按请求身份关联，不能用订单号猜测多次撤单。"""
        detail = insert_error_detail(info)
        if detail is not None:
            rid = request_id or getattr(row, "RequestID", None)
            self._control_request_result(rid, "broker_rejected", {"error_id": detail[0], "error_msg": detail[1]})
            link = getattr(self, "_control_request_links", {}).get(rid)
            if link is not None:
                self.account_orders.activity.record_failure(
                    "cancel_order",
                    str(getattr(row, "InstrumentID", "")),
                    link[4],
                    detail[1],
                    event_id=f"ctp-cancel-{rid}",
                    outcome="rejected",
                )
        self._log_error(info, "撤单")

    @override
    def handle_termination_checkpoint(self, symbol: str | None = None) -> None:
        """费用查询等待额度时也检查本线程期限，不修改共享 guard 回调。"""
        super().handle_termination_checkpoint(symbol)
        state = getattr(self, "_catalog_query_state", None)
        deadline = getattr(state, "fee_deadline", None)
        if deadline is not None and clock_monotonic() >= deadline:
            raise TimeoutError("手续费查询超时")

    @override
    def get_account_control_guard(self) -> AccountControlGuard | None:
        """公开操作进入额度检查前即绑定会话总量，覆盖人工操作的局部 guard。"""
        guard = super().get_account_control_guard()
        if guard is not None:
            guard.share_quota_state(self._control_quota_state)
        return guard

    @override
    def set_account_control_guard(self, guard: AccountControlGuard | None) -> None:
        """新执行读取基线后合入会话总量，不重置其他上下文已占用的额度。"""
        if guard is not None:
            guard.share_quota_state(self._control_quota_state)
        super().set_account_control_guard(guard)

    def _send_trader_request(self, operation, name, req, *, symbol=None, before_send=None) -> int:
        """经通用账户控制发送一次 TraderApi 请求，不在适配层重试。"""
        guard = getattr(self._catalog_query_state, "guard", None) or self.get_account_control_guard()
        share = getattr(guard, "share_quota_state", None)
        if callable(share):
            share(self._control_quota_state)

        return run_controlled_call(
            guard=guard,
            operation=operation,
            symbol=symbol,
            metadata={"request_name": name, "group": "ctp_td_global"},
            call=lambda: self._call_trader_request(name, req, before_send=before_send),
            success_outcome="accepted",
        )

    def _report_stage_failure(self, stage, error, *, timeout=False):
        """按当前阶段提供展示文案，结算与查询仍保留原有故障边界。"""
        name = "认证" if stage is self._auth else "交易登录" if stage is self._login else "行情登录"
        report_ctp_stage(connection_monitor(self), error, name, timeout=timeout)

    @staticmethod
    def _initialization_problem(kind):
        """只确认原生 API 初始化阶段，不推断登录或凭证状态。"""
        return connection_problem(
            f"{kind}连接初始化失败",
            f"CTP {kind} API 创建、注册或初始化过程失败。",
            facts={"失败阶段": f"{kind} API 初始化"},
            facts_title="初始化阶段",
            steps_title="初始化检查",
            steps=(f"检查{kind}前置配置和本机 SDK 运行条件。",),
        )

    @staticmethod
    def _session_problem(label, summary, facts):
        """会话失效后需要创建新实例，阅读弹窗不会自动重连。"""
        return connection_problem(
            label,
            summary,
            facts={**facts, "会话状态": "已失效，需重建"},
            facts_title="会话信息",
            steps_title="重建会话",
            steps=("检查前置服务、网络和会话交易日信息。", "处理故障后创建新实例重新连接；发起连接本身不会确认恢复。"),
        )

    def _wait(self, stage, name):
        if not stage.done.wait(self._timeout):
            error = TimeoutError(f"{name}超时")
            if stage is not self._settlement:
                self._report_stage_failure(stage, error, timeout=True)
            self._invalidate_connection(error)
            raise error
        if stage.error:
            raise stage.error
        self._catalog_progress(name)

    def _install_catalog(self, rows: Mapping[str, Instrument]) -> None:
        self._instruments = {row.symbol: row.ctp() for row in self._catalog_records.values()}

    def _catalog_related_symbols(self) -> list[str]:
        c = self._config()
        positions = self._query("ReqQryInvestorPosition", build_query_positions(c))
        orders = self._query("ReqQryOrder", build_query_orders(c))
        return self._position_catalog_symbols(positions) + [
            str(row.InstrumentID)
            for row in orders
            if getattr(row, "InstrumentID", "") and int(getattr(row, "VolumeTotal", 0) or 0) > 0
        ]

    @staticmethod
    def _position_catalog_symbols(positions) -> list[str]:
        result = []
        for row in positions:
            if int(getattr(row, "Position", 0) or 0) <= 0:
                continue
            for leg in split_combination_position(row) or [row]:
                symbol = leg.get("InstrumentID", "") if isinstance(leg, dict) else getattr(leg, "InstrumentID", "")
                if symbol:
                    result.append(str(symbol))
        return result

    def _query_catalog(self, symbols: list[str] | None, timeout: float | None) -> list[Instrument]:
        """复用当前会话；全量发一次，按需由 CTP 逐个指定合约。"""
        if symbols is not None:
            for item in symbols:
                exchange, separator, local = item.partition(".")
                if separator:
                    if exchange not in {"SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"}:
                        raise ValueError("不支持的交易所代码")
                    validate_instrument(local)
                else:
                    validate_instrument(item)
        state = self._catalog_query_state
        state.active = True
        state.deadline = catalog_deadline(timeout)
        records = []
        try:
            for symbol in symbols if symbols is not None else [None]:
                request = td.CThostFtdcQryInstrumentField()
                if symbol is not None:
                    exchange, separator, local = symbol.partition(".")
                    request.InstrumentID = local if separator else exchange
                    if separator:
                        request.ExchangeID = exchange
                response_timeout = catalog_remaining(state.deadline)
                rows = self._query("ReqQryInstrument", request, timeout=response_timeout)
                catalog_remaining(state.deadline)
                records.extend(
                    record
                    for row in rows
                    if (record := from_ctp(vars(row))) is not None and (symbols is not None or record.kind != "COMBINE")
                )
            return records
        finally:
            state.active = False
            state.deadline = state.guard = None

    @contextmanager
    def _catalog_query_control(self):
        """取得查询权后才读取控制基线，记录刷盘完成后释放查询权。"""
        state = getattr(self, "_catalog_query_state", None)
        factory = getattr(self, "_catalog_control_factory", None)
        if state is None or not getattr(state, "active", False) or factory is None:
            yield
            return
        with factory(self._catalog_checkpoint) as guard:
            state.guard = guard
            try:
                yield
            finally:
                state.guard = None

    def _catalog_progress(self, phase: str, *, throttled: bool = False) -> None:
        if self._ready:
            return
        now = time.monotonic()
        if not throttled or now - self._catalog_progress_at >= 1:
            self._instrument_provider.progress(phase)
            self._catalog_progress_at = now

    def _catalog_checkpoint(self):
        self._require_active_connection()
        state = getattr(self, "_catalog_query_state", None)
        fee_deadline = getattr(state, "fee_deadline", None)
        if fee_deadline is not None and clock_monotonic() >= fee_deadline:
            raise TimeoutError("手续费查询超时")
        if getattr(state, "active", False):
            if state.deadline is not None and clock_monotonic() >= state.deadline:
                raise TimeoutError("合约资料查询超时")
            return
        preparation = getattr(self, "_catalog_prepare_deadline", None)
        if preparation is not None and clock_monotonic() >= preparation:
            raise TimeoutError("等待合约资料超时")
        runtime = self.get_active_execution_runtime()
        if runtime is not None:
            if not cleanup_query_allowed(runtime):
                runtime.handle_termination_checkpoint()

    def _initialize_connection(self, account_config):
        validate_config(account_config)
        with self._lock:
            self._require_active_connection()
            if self._connection_started:
                raise CtpSessionRecoveryRequired("CTP 实例不能重复初始化，请创建新实例")
            self._connection_started = True
        if not isinstance(account_config, CTPAccountConfig):
            raise TypeError("CTPExecutor requires CTPAccountConfig")
        required = ("broker_id", "investor_id", "password", "td_front", "md_front", "app_id", "auth_code")
        missing = [x for x in required if not getattr(account_config, x, None)]
        if missing:
            error = ValueError(f"CTP 配置缺少必填字段: {', '.join(missing)}")
            report_ctp_failure(
                connection_monitor(self),
                error,
                "连接配置不完整",
                "CTP 连接配置缺少必填字段。",
                facts={"缺少字段": ", ".join(missing)},
                steps=("补全连接设置中的必填字段。",),
            )
            raise error
        self._timeout = float(getattr(account_config, "query_timeout", 15))
        self._flow_dir = Path(tempfile.mkdtemp(prefix="axile-ctp-"))
        try:
            connection_monitor(self).step("trader_connection", "running")
            path = self._flow_dir / "trader"
            path.mkdir()
            with connection_monitor(self).connecting(
                label="交易连接初始化失败",
                presentation=lambda: self._initialization_problem("交易"),
            ):
                self._trader_api = td.CThostFtdcTraderApi.CreateFtdcTraderApi(str(path) + "/")
                self._trader_spi = TraderSpi(self)
                self._trader_api.RegisterSpi(self._trader_spi)
                self._trader_api.RegisterFront(account_config.td_front)
                self._trader_api.SubscribePrivateTopic(td.THOST_TERT_QUICK)
                self._trader_api.SubscribePublicTopic(td.THOST_TERT_QUICK)
                self._trader_api.Init()
            self._wait(self._auth, "认证")
            self._wait(self._login, "登录")
            connection_monitor(self).step("trader_login", "passed", step_details(self, "trader_login"))
            connection_monitor(self).step("settlement", "running")
            self._ensure_settlement_confirmed()
            connection_monitor(self).step("settlement", "passed", step_details(self, "settlement"))
            if self._validation_catalog:
                connection_monitor(self).step("instruments", "running")
                self._instruments = self._validation_catalog()
                connection_monitor(self).step("instruments", "passed", step_details(self, "instruments"))
            connection_monitor(self).step("market_login", "running")
            path = self._flow_dir / "market"
            path.mkdir()
            with connection_monitor(self).connecting(
                label="行情连接初始化失败",
                presentation=lambda: self._initialization_problem("行情"),
            ):
                self._market_api = md.CThostFtdcMdApi.CreateFtdcMdApi(str(path) + "/")
                self._market_spi = MarketSpi(self)
                self._market_api.RegisterSpi(self._market_spi)
                self._market_api.RegisterFront(account_config.md_front)
                self._market_api.Init()
            self._wait(self._md_login, "行情登录")
            connection_monitor(self).step("market_login", "passed", step_details(self, "market_login"))
            if not self._validation_catalog:
                self._instrument_provider.bind(self._query_catalog, channel="ctp")
            self._reconcile_session()
            with self._lock:
                self._require_active_connection()
                # 查询与实时回报不构成柜台原子快照；合并查询期间到达的成交，
                # 以交易所、TradeID 去重后再公布恢复屏障。
                trades = self._recovery_snapshot["trades"]
                trades.extend(self._convert_trade(row) for row in self._startup_trades)
                self._recovery_snapshot["trades"] = list(
                    {(trade.extra.get("exchange_id"), trade.trade_id): trade for trade in trades}.values()
                )
                self._startup_trades.clear()
                for trade in self._recovery_snapshot["trades"]:
                    self.account_orders.activity.observe_trade(trade)
                self._ready = True
            connection_monitor(self).step("channel_ready", "passed", step_details(self, "channel_ready"))
        except Exception:
            self.close()
            raise

    def _trader_connected_cb(self):
        with self._lock:
            if self._logout_in_progress and not self._closed:
                self.logger.info("主动登出期间收到 CTP 交易 OnFrontConnected，跳过认证和登录")
                return
            if self._closed or self._invalid_reason or self._logout_in_progress:
                return
            if self._trader_connected:
                return
            self._trader_connected = True
        connection_monitor(self).step("trader_connection", "passed", step_details(self, "trader_connection"))
        connection_monitor(self).step("authenticate", "running")
        req = build_authenticate(self._config())
        self._send_stage(self._auth, "authenticate", "ReqAuthenticate", req)

    def _authenticated(self, row, info, request_id):
        if not self._finish_stage(self._auth, info, "认证", request_id):
            return
        connection_monitor(self).step("authenticate", "passed", step_details(self, "authenticate"))
        connection_monitor(self).step("trader_login", "running")
        req = build_trader_login(self._config())
        self._send_stage(self._login, "trader_login", "ReqUserLogin", req)

    def _logged_in(self, row, info, request_id):
        with self._lock:
            if not self._accept_stage(self._login, request_id):
                return
            if self._error(info, "登录"):
                self._finish_stage(self._login, info, "登录", request_id)
                return
            if row is None or not str(row.TradingDay).isdigit() or len(str(row.TradingDay)) != 8:
                self._invalidate_connection(
                    "交易登录缺少有效交易日",
                    connection_failure=True,
                    label="交易会话异常",
                    presentation=lambda: self._session_problem(
                        "交易会话异常", "交易登录应答没有有效交易日。", {"失败阶段": "交易登录应答"}
                    ),
                )
                return
            self._trading_day = str(row.TradingDay)
            self._front_id = int(row.FrontID)
            self._session_id = int(row.SessionID)
            self._order_ref = int(row.MaxOrderRef or 0) + 1
            self.connection_records["trader_login"] = _copy_native_row(row)
            self._finish_stage(self._login, info, "登录", request_id)

    def _ensure_settlement_confirmed(self):
        """查询柜台状态，并仅在当前交易日未确认时发送确认。"""
        c = self._config()
        rows = self._query("ReqQrySettlementInfoConfirm", build_query_settlement_confirm(c))
        for row in rows:
            if str(getattr(row, "ConfirmDate", "") or "") == self._trading_day:
                self.settlement_confirmation = self._settlement_record(row, "existing")
                self._settlement.done.set()
                return
        req = build_settlement_confirm(self._config())
        self._send_stage(self._settlement, "confirm_settlement", "ReqSettlementInfoConfirm", req)
        self._wait(self._settlement, "结算确认")

    @staticmethod
    def _settlement_record(row, source):
        """仅提取确认记录字段，不传递原生对象或登录凭据。"""
        fields = {
            "broker_id": "BrokerID",
            "investor_id": "InvestorID",
            "confirm_date": "ConfirmDate",
            "confirm_time": "ConfirmTime",
            "settlement_id": "SettlementID",
            "account_id": "AccountID",
            "currency_id": "CurrencyID",
        }
        return {"source": source, **{key: getattr(row, field, None) for key, field in fields.items()}}

    def _settled(self, info, request_id, row=None):
        with self._lock:
            if self._accept_stage(self._settlement, request_id) and not self._error(info, "结算确认"):
                # 先保存记录再唤醒等待线程，确保 passed 事件始终携带详情。
                self.settlement_confirmation = self._settlement_record(row, "submitted")
            self._finish_stage(self._settlement, info, "结算确认", request_id)

    def _market_connected_cb(self):
        with self._lock:
            if self._logout_in_progress and not self._closed:
                self.logger.info("主动登出期间收到 CTP 行情 OnFrontConnected，跳过登录")
                return
            if self._closed or self._invalid_reason or self._logout_in_progress or self._market_connected:
                return
            self._market_connected = True
        req = build_market_login(self._config())
        try:
            with self._lock:
                self._require_active_connection()
                self._md_login.request_id = self._next_id()
                self._check(self._market_api.ReqUserLogin(req, self._md_login.request_id), "行情登录")
        except Exception as e:
            report_ctp_stage(connection_monitor(self), e, "行情登录")
            self._invalidate_connection(e)

    def _market_logged_in(self, row, info, request_id):
        with self._lock:
            if (
                request_id == 0
                and not self._closed
                and not self._invalid_reason
                and self._md_login.request_id is not None
                and not self._md_login.done.is_set()
            ):
                request_id = self._md_login.request_id
            if not self._accept_stage(self._md_login, request_id):
                return
            if not self._error(info, "行情登录") and str(getattr(row, "TradingDay", "")) != self._trading_day:
                self._invalidate_connection(
                    "行情登录交易日与交易会话不一致",
                    connection_failure=True,
                    label="会话交易日异常",
                    presentation=lambda: self._session_problem(
                        "会话交易日异常",
                        "行情登录交易日与交易会话不一致。",
                        {"交易会话交易日": self._trading_day, "行情交易日": str(getattr(row, "TradingDay", ""))},
                    ),
                )
                return
            if (
                self._finish_stage(self._md_login, info, "行情登录", request_id)
                and self._auth.done.is_set()
                and self._auth.error is None
                and self._login.done.is_set()
                and self._login.error is None
            ):
                connection_monitor(self).succeeded()

    def _user_logged_out(self, kind, row, info, request_id, is_last):
        """区分匹配的主动登出应答与活跃会话的非预期登出。"""
        with self._lock:
            error_id = int(getattr(info, "ErrorID", 0) or 0)
            message = str(getattr(info, "ErrorMsg", "") or "")
            detail = (
                f"CTP {kind} OnRspUserLogout request_id={request_id} "
                f"is_last={is_last} ErrorID={error_id} ErrorMsg={message}"
            )
            if self._closed:
                self.logger.debug("迟到登出应答: {}", detail)
                return
            pending = self._logout_pending.get(kind)
            if self._logout_in_progress:
                self.logger.info("收到主动登出应答: {}", detail)
                if pending is None or pending.request_id != request_id or pending.done.is_set():
                    self.logger.debug("未匹配或重复的主动登出应答: {}", detail)
                    return
                if error_id:
                    pending.error = CtpRequestError(detail, error_id=error_id)
                    self.logger.warning("主动登出失败: {}", detail)
                    pending.done.set()
                elif is_last:
                    self.logger.info("主动登出成功: {}", detail)
                    pending.done.set()
                return
            if self._invalid_reason:
                self.logger.debug("失效会话登出应答: {}", detail)
                return
            if error_id:
                self.logger.warning("登出失败应答: {}", detail)
                return
            self.logger.warning("会话意外登出: {}", detail)
            label = f"{kind}会话已登出"
            self._invalidate_connection(
                CtpSessionRecoveryRequired(detail, execution_error=f"CTP {label}", reason_code="CTP_USER_LOGOUT"),
                connection_failure=True,
                label=label,
                presentation=lambda: connection_problem(
                    label,
                    f"已收到 CTP {kind}登出成功应答，当前账户会话已失效。",
                    facts={
                        "登出对象": kind,
                        "原生回调": "OnRspUserLogout",
                        "请求编号": request_id,
                        "应答结果": "成功（ErrorID=0）",
                        "会话状态": "已失效，需重建",
                    },
                    steps=("重新连接账户，连接完成后核对资产与委托。",),
                ),
            )

    def _logout_response_error(self, kind, info, request_id):
        """保留通用错误应答，匹配登出请求时结束等待，绝不伪造登出成功。"""
        with self._lock:
            if self._closed:
                return
            error = self._error(info, f"{kind}请求")
            if error is None:
                return
            self.logger.warning("CTP {} OnRspError request_id={} error={}", kind, request_id, error)
            pending = self._logout_pending.get(kind)
            if self._logout_in_progress and pending is not None and request_id == pending.request_id:
                if not pending.done.is_set():
                    pending.error = error
                    pending.done.set()

    def graceful_logout(self):
        """仅请求交易登出，保留原生回调证据，最多等待五秒后交给宿主释放。"""
        with self._lock:
            if self._closed or self._invalid_reason or self._logout_in_progress:
                self.logger.debug("跳过主动登出：会话已关闭、失效或正在登出")
                return
            self._logout_in_progress = True
            self._ready = self._monitoring = False
            # 主动关闭期间保留观察能力；预期回调在各入口单独处理，
            # 不通过提前关闭 monitor 隐藏柜台实际发生的事件。
            self._fail_waiters(ConnectionError("CTP 账户正在主动断开"))
            deadline = clock_monotonic() + 5.0
            api, login = self._trader_api, self._login
            if api is None or not login.done.is_set() or login.error is not None:
                self.logger.info("跳过 CTP 交易主动登出：没有已登录通路")
                return
            pending = _Stage(threading.Event(), request_id=self._next_id())
            self._logout_pending["交易"] = pending
            try:
                self.logger.info("主动登出请求: CTP 交易 ReqUserLogout request_id={}", pending.request_id)
                code = api.ReqUserLogout(build_logout(self._config()), pending.request_id)
                self.logger.info("主动登出请求返回: CTP 交易 request_id={} return_code={}", pending.request_id, code)
                self._check(code, "交易登出")
            except Exception as exc:
                pending.error = exc
                pending.done.set()
                self.logger.warning("主动登出请求未受理: CTP 交易 error={}", exc)
        if not get_default_clock().event_wait(pending.done, max(0.0, deadline - clock_monotonic())):
            with self._lock:
                if not pending.done.is_set():
                    pending.error = TimeoutError("CTP 交易未收到登出成功应答")
                    pending.done.set()
                    if self._logout_disconnect_reason is not None:
                        self.logger.info(
                            "主动登出结束: CTP 交易已收到 OnFrontDisconnected reason={}，未收到 OnRspUserLogout；即将释放 API",
                            self._logout_disconnect_reason,
                        )
                    else:
                        self.logger.warning("主动登出超时: CTP 交易 request_id={}", pending.request_id)

    def _disconnected(self, kind, reason):
        with self._lock:
            if kind == "交易":
                self._trader_connected = False
            else:
                self._market_connected = False
            if self._logout_in_progress and not self._closed:
                self.logger.info("主动登出期间收到 CTP {} OnFrontDisconnected reason={}", kind, reason)
                if kind == "交易":
                    self._logout_disconnect_reason = reason
                return
            self._connection_error = f"{kind}前置已断开"
            self._invalidate_connection(
                CtpSessionRecoveryRequired(
                    f"CTP {kind}前置断线: {reason}",
                    execution_error=self._connection_error,
                    reason_code=str(reason),
                ),
                connection_failure=True,
                label=f"{kind}前置断开",
                presentation=lambda: self._session_problem(
                    f"{kind}前置断开",
                    f"已收到 CTP {kind}前置断线回调，本会话已失效。",
                    {"断开对象": f"{kind}前置", "原生断线原因": reason},
                ),
            )

    def _invalidate_connection(self, reason, *, connection_failure=False, label=None, presentation=None):
        """永久撤销就绪状态；保留首个失效原因及其可读错误契约。"""
        with self._lock:
            if self._closed:
                return
            if not self._invalid_reason:
                self._invalid_reason = str(reason)
                self._connection_failure = CtpSessionRecoveryRequired(
                    self._invalid_reason,
                    execution_error=getattr(reason, "execution_error", None)
                    or getattr(self, "_connection_error", None),
                    return_code=getattr(reason, "return_code", None),
                    error_id=getattr(reason, "error_id", None),
                    reason_code=getattr(reason, "reason_code", None),
                )
                if connection_failure or (isinstance(reason, BaseException) and connection_error_scope(reason)):
                    # 先撤销会话，再发布原始故障，避免持久化期间旧会话继续接收请求。
                    source = reason if isinstance(reason, BaseException) else self._connection_failure
                    connection_monitor(self).failed(source, label=label, presentation=presentation)
                    preserve_connection_failure(self._connection_failure, source)
            self.account_orders.invalidate(str(reason))
            self._ready = False
            self._monitoring = False
            self._quotes.clear()
            self._valuation_quotes.clear()
            self._fail_waiters(self._connection_failure)

    def _require_active_connection(self):
        """禁止失效实例继续访问原生 API，包括尚在排队的请求。"""
        if self._closed or self._invalid_reason or self._logout_in_progress:
            failure = getattr(self, "_connection_failure", None)
            if failure is not None:
                raise failure
            raise CtpSessionRecoveryRequired(
                self._invalid_reason or "CTPExecutor 已关闭或正在主动断开",
                execution_error=getattr(self, "_connection_error", None),
            )

    def _require_session_ready(self, symbol=None):
        """检查会话就绪及目标品种在本实例收到的新行情。"""
        self._require_active_connection()
        if not self._verify_connection():
            raise CtpRequestError("CTP 会话尚未完成认证、登录、结算、元数据和对账")
        if symbol and (
            symbol not in self._quotes or symbol not in self._subscription_acks or symbol in self._subscription_errors
        ):
            raise CtpRequestError(f"CTP {symbol} 尚未收到本会话订阅的新行情")

    def _require_new_order_ready(self, symbol=None):
        """未归属成交只阻止新增委托，不阻止健康会话撤销已知订单。"""
        self._require_session_ready()
        runtime = self.get_active_execution_runtime()
        if runtime is not None:
            runtime.handle_termination_checkpoint(symbol)
        if self._unassociated_trades or self._ambiguous_exchange_orders:
            raise CtpRequestError("CTP 存在未归属成交或歧义订单关联，禁止新单")
        if symbol:
            self._require_session_ready(symbol)

    def _send_stage(self, stage, operation, name, req):
        """在原生发送前绑定阶段请求编号，兼容同步回调的 SDK 替身。"""
        try:
            self._send_trader_request(operation, name, req, before_send=lambda rid: setattr(stage, "request_id", rid))
        except Exception as e:
            if stage is not self._settlement:
                self._report_stage_failure(stage, e)
            self._invalidate_connection(e)

    def _accept_stage(self, stage, request_id):
        accepted = (
            not self._closed
            and not self._invalid_reason
            and not self._logout_in_progress
            and request_id is not None
            and stage.request_id == request_id
            and not stage.done.is_set()
        )

        if not accepted:
            name = next(
                (name for name in ("_auth", "_login", "_settlement", "_md_login") if getattr(self, name) is stage),
                "unknown",
            )
            self.logger.debug(
                f"CTP 阶段应答拒绝: stage={name} expected={stage.request_id} actual={request_id} "
                f"closed={self._closed} invalid={bool(self._invalid_reason)} done={stage.done.is_set()}"
            )
        return accepted

    def _finish_stage(self, stage, info, name, request_id):
        """只接受本实例未完成请求的首次响应，错误使整个实例失效。"""
        with self._lock:
            if not self._accept_stage(stage, request_id):
                return False
            error = self._error(info, name)
            if error:
                if stage is not self._settlement:
                    self._report_stage_failure(stage, error)
                self._invalidate_connection(error)
                return False
            stage.done.set()
            return True

    def _fail_waiters(self, error):
        for s in (self._auth, self._login, self._settlement, self._md_login):
            if not s.done.is_set():
                s.error = error
                s.done.set()
        with self._lock:
            for p in self._pending_queries.values():
                p.error = error
                p.done.set()

    @contextmanager
    def _query_slot(self, timeout: float | None):
        """复用会话互斥锁，等待期间检查取消及来源退出。"""
        deadline = None if timeout is None else clock_monotonic() + timeout
        while not self._query_lock.acquire(blocking=False):
            self._catalog_checkpoint()
            remaining = 0.01 if deadline is None else deadline - clock_monotonic()
            if remaining <= 0:
                raise TimeoutError("等待 CTP 查询权超时")
            get_default_clock().sleep(min(0.01, remaining))
        try:
            yield
        finally:
            self._query_lock.release()

    def _query(self, name, req, *, symbol=None, timeout=None):
        with connection_monitor(self).accessing():
            return self._query_connected(name, req, symbol=symbol, timeout=timeout)

    def _query_connected(self, name, req, *, symbol=None, timeout=None):
        wait_seconds = self._timeout if timeout is None else timeout
        state = self._catalog_query_state
        slot_timeout = None if getattr(state, "active", False) and state.deadline is None else wait_seconds
        with self._query_slot(slot_timeout), self._catalog_query_control():
            operation = {
                "ReqQryInstrument": "query_instruments",
                "ReqQryInstrumentCommissionRate": "query_commission_rates",
                "ReqQryTradingAccount": "query_account",
                "ReqQryInvestorPosition": "query_positions",
                "ReqQryDepthMarketData": "query_depth_market_data",
                "ReqQryOrder": "query_orders",
                "ReqQryTrade": "ctp_query_trades",
                "ReqQrySettlementInfoConfirm": "query_settlement_status",
            }[name]
            pending = _PendingQuery([], threading.Event())
            pending.timeout = wait_seconds
            if name == "ReqQryInstrument":
                pending.idle = QueryIdleClock(clock_monotonic)
            pending_rid = None

            def register_pending(rid):
                nonlocal pending_rid
                pending_rid = rid
                if pending.idle is not None:
                    pending.idle.sent()
                self._pending_queries[rid] = pending

            try:
                self._send_trader_request(operation, name, req, symbol=symbol, before_send=register_pending)
                if pending.idle is not None:
                    pending.idle.sent()
            except Exception:
                if pending_rid is not None:
                    self._pending_queries.pop(pending_rid, None)
                raise
            assert pending_rid is not None
            try:
                self._wait_query(pending, name, timeout=wait_seconds)
                if pending.error:
                    raise pending.error
                self._catalog_progress(f"{name}完成")
                return list(pending.rows)
            finally:
                pending.done.set()
                self._pending_queries.pop(pending_rid, None)

    def _wait_query(self, pending, name, *, timeout=None):
        wait_seconds = self._timeout if timeout is None else timeout
        deadline = clock_monotonic() + wait_seconds
        if pending.idle is None:
            while not pending.done.is_set() and not get_default_clock().event_wait(pending.done, 0.05):
                self._catalog_checkpoint()
                if clock_monotonic() >= deadline:
                    raise TimeoutError(f"{name}超时")
            return
        while not pending.done.is_set() and not get_default_clock().event_wait(pending.done, 0.05):
            self._catalog_checkpoint()
            if pending.idle.timed_out(wait_seconds, pending.done):
                raise TimeoutError(f"{name}超时：连续 {wait_seconds:g} 秒无回调")
        self.logger.info(
            "CTP 合约查询完成 | records={} max_idle={:.3f}s local_processing={:.3f}s",
            len(pending.rows),
            pending.idle.max_idle,
            pending.idle.processing_seconds,
        )

    def _query_response(self, row, info, rid, last):
        p = self._pending_queries.get(rid)
        if not p or p.done.is_set():
            return
        if p.idle is not None and not p.idle.enter(self._timeout if p.timeout is None else p.timeout):
            return
        try:
            with self._lock:
                if p.done.is_set():
                    return
                p.error = self._error(info, "查询")
                if row is not None and not p.error:
                    p.rows.append(_copy_native_row(row))
                if p.idle is not None:
                    self._catalog_progress("合约记录", throttled=True)
                if last or p.error:
                    p.done.set()
        except Exception as exc:
            p.error = exc
            p.done.set()
        finally:
            if p.idle is not None:
                p.idle.leave()

    @override
    def _verify_connection(self):
        with self._lock:
            return (
                self._ready
                and not self._closed
                and not self._invalid_reason
                and self._trader_connected
                and self._market_connected
            )

    def _reconcile_session(self):
        """就绪前取得当前交易日完整查询快照，并恢复旧会话的订单关联。

        Notes
        -----
        空持仓、订单或成交是合法结果；资金空结果和无法关联的成交不是。
        这是柜台查询屏障，不承诺多次查询在柜台侧构成原子快照。
        """
        c = self._config()
        watermark = self.account_orders.begin_sync()
        orders = self._query_session_rows("query_orders", "ReqQryOrder", build_query_orders(c))
        for row in orders:
            self._remember_order(row)
            self._catalog_progress("对账记录", throttled=True)
        self.connection_records["query_orders"] = orders
        connection_monitor(self).step("query_orders", "passed", step_details(self, "query_orders"))
        self.account_orders.replace([self._remember_order(row) for row in orders], watermark)
        trades = self._query_session_rows("query_trades", "ReqQryTrade", build_query_trades(c, ""))
        converted_trades = []
        for row in trades:
            converted_trades.append(self._convert_trade(row))
            self._catalog_progress("对账记录", throttled=True)
        self.connection_records["query_trades"] = trades
        connection_monitor(self).step("query_trades", "passed", step_details(self, "query_trades"))
        positions = self._query_session_rows("query_positions", "ReqQryInvestorPosition", build_query_positions(c))
        if not self._validation_catalog:
            self._ensure_instrument_symbols(self._position_catalog_symbols(positions))
        self.connection_records["query_positions"] = positions
        connection_monitor(self).step("query_positions", "passed", step_details(self, "query_positions"))
        accounts = self._query_session_rows("query_account", "ReqQryTradingAccount", build_query_account(c))
        if not accounts:
            raise CtpRequestError("会话对账资金查询返回空结果")
        self.connection_records["query_account"] = accounts
        connection_monitor(self).step("query_account", "passed", step_details(self, "query_account"))
        connection_monitor(self).step("channel_ready", "running")
        quotes, quote_errors = self._position_valuation_quotes(positions)
        self._recovery_snapshot = {
            "trading_day": self._trading_day,
            "assets": account_to_unified(
                accounts[-1],
                positions,
                self._instruments,
                quotes=quotes,
                quote_errors=quote_errors,
                progress=lambda: self._catalog_progress("持仓转换", throttled=True),
            ),
            "orders": orders,
            "trades": converted_trades,
        }

    def _query_session_rows(self, step, request_name, request):
        """查询并逐阶段校验交易日，让失败归属于实际执行的检查项。"""
        connection_monitor(self).step(step, "running")
        rows = self._query(request_name, request)
        for row in rows:
            day = str(getattr(row, "TradingDay", "") or "")
            if day and day != self._trading_day:
                raise CtpRequestError(f"会话对账交易日不一致: expected={self._trading_day}, actual={day}")
            self._catalog_progress("对账记录", throttled=True)
        return rows

    def _remember_order(self, row):
        """保存柜台原始会话键及交易所键，供成交和撤单恢复使用。"""
        day = str(getattr(row, "TradingDay", "") or "")
        if day and day != self._trading_day:
            raise CtpRequestError("订单交易日与当前 CTP 会话不一致")
        order = order_to_unified(
            row, trading_day=self._trading_day, front_id=self._front_id, session_id=self._session_id
        )
        with self._lock:
            self._order_keys[order.order_id] = {
                k: order.extra.get(k, "")
                for k in ("order_ref", "front_id", "session_id", "exchange_id", "order_sys_id")
            }
            key = (self._trading_day, order.extra.get("exchange_id"), order.extra.get("order_sys_id"))
            if all(key):
                previous = self._exchange_orders.get(key)
                if previous is not None and previous.order_id != order.order_id:
                    self._ambiguous_exchange_orders.add(key)
                    raise CtpRequestError(f"CTP 订单关联有歧义: {key}")
                self._exchange_orders[key] = order
        return order

    def _convert_trade(self, row):
        """成交必须由交易所订单键解析会话，禁止回退为当前 SessionID。"""
        day = str(getattr(row, "TradingDay", "") or "")
        if day and day != self._trading_day:
            raise CtpRequestError("成交交易日与当前 CTP 会话不一致")
        key = (
            day or self._trading_day,
            str(getattr(row, "ExchangeID", "")),
            str(getattr(row, "OrderSysID", "")).strip(),
        )
        with self._lock:
            order = self._exchange_orders.get(key)
        if order is None or key in self._ambiguous_exchange_orders:
            with self._lock:
                self._unassociated_trades[self._native_trade_key(row)] = _copy_native_row(row)
            if key in self._ambiguous_exchange_orders:
                raise CtpRequestError(f"CTP 成交缺少唯一可验证的订单关联: {key}")
            raise _TradeAssociationPending(f"CTP 成交缺少唯一可验证的订单关联: {key}")
        trade = trade_to_unified(
            row,
            trading_day=self._trading_day,
            front_id=int(order.extra["front_id"]),
            session_id=int(order.extra["session_id"]),
            resolved_order_id=order.order_id,
        )
        return trade.model_copy(update={"order_id": order.order_id})

    @override
    def _check_trading_time(self) -> bool:
        """市场缝用钟判断；盘中品种时段仍由 CTP 编排器负责。"""
        return is_within_possible_china_futures_session(datetime.now(_SHANGHAI))

    @override
    def _execution_engine(self) -> ExecutionEngine:
        return CtpExecutionEngine(self, self.require_execution_runtime())

    def _get_ctp_session_block_reason(self, symbol: str) -> str | None:
        """返回 CTP 合约当前不可交易时的稳定原因码。"""
        instrument = self._instruments.get(symbol)
        if instrument is None:
            return "CTP.SESSION.NO_METADATA"
        exchange_id = str(getattr(instrument, "ExchangeID", "") or "")
        product_id = str(getattr(instrument, "ProductID", "") or "")
        if not exchange_id or not product_id:
            return "CTP.SESSION.NO_METADATA"
        if getattr(instrument, "ProductClass", None) != td.THOST_FTDC_PC_Futures:
            return "CTP.SESSION.NO_SESSION_TABLE"
        sessions = get_ctp_product_sessions(exchange_id, product_id)
        if not sessions:
            return "CTP.SESSION.NO_SESSION_TABLE"
        now = clock_now(tz=_SHANGHAI)
        calendar = getattr(self, "_trading_calendar", None)
        calendar_id = getattr(self, "_channel_calendar_id", None)
        if calendar is None or calendar_id is None:
            return "CTP.SESSION.CALENDAR_UNAVAILABLE"
        return decide_ctp_product_session(
            sessions,
            now=now,
            calendar_is_open=lambda day: calendar.is_open(calendar_id, day),
        ).reason_code

    @override
    def _normalize_connected_standard_input(self, standard_input: UnifiedStandardInput) -> UnifiedStandardInput:
        """使用 CTP 合约目录规范化郑商所四位年份期货/期权代码。"""
        return standard_input.model_copy(
            update={
                "curr_target": self._normalize_symbol_mapping("curr_target", standard_input.curr_target),
                "last_target": self._normalize_symbol_mapping("last_target", standard_input.last_target),
                "symbol_algorithms": self._normalize_symbol_mapping(
                    "symbol_algorithms", standard_input.symbol_algorithms
                ),
                "trade_rules": self._normalize_symbol_mapping("trade_rules", standard_input.trade_rules),
                "forbidden_symbols": self._normalize_symbol_list(standard_input.forbidden_symbols),
                "risk_symbols": self._normalize_symbol_list(standard_input.risk_symbols),
            }
        )

    def _normalize_symbol_mapping(self, field: str, values: Mapping[str, _ValueT]) -> dict[str, _ValueT]:
        normalized: dict[str, _ValueT] = {}
        sources: dict[str, str] = {}
        for raw_symbol, value in values.items():
            symbol = self._normalize_ctp_symbol(raw_symbol)
            if symbol in normalized and normalized[symbol] != value:
                raise ValueError(
                    f"CTP 输入字段 {field} 中的代码 {sources[symbol]} 与 {raw_symbol} 都对应 {symbol}，但配置不一致"
                )
            normalized[symbol] = value
            sources.setdefault(symbol, raw_symbol)
        return normalized

    def _normalize_symbol_list(self, values: list[str]) -> list[str]:
        return list(dict.fromkeys(self._normalize_ctp_symbol(symbol) for symbol in values))

    def _normalize_ctp_symbol(self, symbol: str) -> str:
        if "." in symbol:
            record = self._catalog_record(symbol)
            if record is not None:
                return record.symbol
        if symbol in self._instruments:
            return symbol
        trading_day = str(getattr(self, "_trading_day", ""))
        reference_year = (
            int(trading_day[:4]) if len(trading_day) >= 4 and trading_day[:4].isdigit() else datetime.now().year
        )
        native_symbol = canonicalize_cn_futures_symbol(symbol, reference_year=reference_year)
        if native_symbol == symbol:
            return symbol
        instrument = self._instruments.get(native_symbol)
        if instrument is None or str(getattr(instrument, "ExchangeID", "")) != "CZCE":
            return symbol
        want_class = td.THOST_FTDC_PC_Options if czce_is_option_instrument(native_symbol) else td.THOST_FTDC_PC_Futures
        if getattr(instrument, "ProductClass", None) != want_class:
            return symbol
        return native_symbol

    def _query_commission_rates(self, symbol):
        """有界查询当前账户的实际合约费率；空响应不推断成零。"""
        from axile.executor.algorithms.utils.clock import clock_now_iso
        from axile.executor.futures_close_policy import CommissionRates

        validate_instrument(symbol)
        config = self._config()
        validate_config(config)
        req = td.CThostFtdcQryInstrumentCommissionRateField()
        req.BrokerID, req.InvestorID, req.InstrumentID = config.broker_id, config.investor_id, symbol
        req.ExchangeID = str(getattr(self._instruments.get(symbol), "ExchangeID", ""))
        timeout = min(self._timeout, 3.0)
        state = self._catalog_query_state
        previous = getattr(state, "fee_deadline", None)
        state.fee_deadline = clock_monotonic() + timeout
        try:
            rows = self._query("ReqQryInstrumentCommissionRate", req, symbol=symbol, timeout=timeout)
        finally:
            state.fee_deadline = previous
        product = "".join(c for c in symbol if c.isalpha()).upper()
        candidates = [row for row in rows if str(getattr(row, "InstrumentID", "")).upper() in {symbol.upper(), product}]
        exact = [row for row in candidates if str(row.InstrumentID).upper() == symbol.upper()]
        selected = exact or candidates
        if len(selected) != 1:
            raise ValueError("账户手续费率缺失或不唯一")
        row = selected[0]
        return CommissionRates(
            yesterday_money=row.CloseRatioByMoney,
            yesterday_volume=row.CloseRatioByVolume,
            today_money=row.CloseTodayRatioByMoney,
            today_volume=row.CloseTodayRatioByVolume,
            queried_at=clock_now_iso(),
        )

    @override
    def get_account_assets(self):
        c = self._config()
        a = build_query_account(c)
        accounts = self._query("ReqQryTradingAccount", a)
        if not accounts:
            raise CtpRequestError("资金查询返回空结果")
        p = build_query_positions(c)
        positions = self._query("ReqQryInvestorPosition", p)
        if not self._validation_catalog:
            self._ensure_instrument_symbols(self._position_catalog_symbols(positions))
        quotes, quote_errors = self._position_valuation_quotes(positions)
        return account_to_unified(accounts[-1], positions, self._instruments, quotes=quotes, quote_errors=quote_errors)

    def _position_valuation_quotes(self, positions):
        """盘中有限等待 tick，休市直接用最近价格，缺价时查询快照。"""
        symbols = []
        for row in positions:
            for leg in split_combination_position(row) or [row]:
                volume = leg.get("Position", 0) if isinstance(leg, dict) else getattr(leg, "Position", 0)
                symbol = str(leg.get("InstrumentID", "") if isinstance(leg, dict) else getattr(leg, "InstrumentID", ""))
                if isinstance(volume, (int, float)) and volume > 0 and symbol in self._instruments:
                    symbols.append(symbol)
        symbols = list(dict.fromkeys(symbols))
        open_symbols = [
            symbol for symbol in symbols if self._get_ctp_session_block_reason(symbol) != "CTP.SESSION.CLOSED"
        ]
        if open_symbols:
            try:
                self.initialize_websocket(open_symbols)
            except (CtpRequestError, TimeoutError, ValueError) as exc:
                self.logger.warning("CTP 持仓行情订阅不可用: %s", exc)
        deadline = time.monotonic() + min(self._timeout, 3.0)
        while open_symbols and time.monotonic() < deadline:
            with self._lock:
                if all(
                    self._fresh_valuation_quote_error(self._valuation_quotes.get(symbol)) is None
                    for symbol in open_symbols
                ):
                    break
            threading.Event().wait(0.05)
        with self._lock:
            cached = {symbol: self._valuation_quotes.get(symbol) for symbol in symbols}
        quotes = {}
        errors = {}
        for symbol in symbols:
            quote = cached[symbol]
            needs_snapshot = valuation_quote_error(quote) is not None or (
                symbol in open_symbols and self._fresh_valuation_quote_error(quote) is not None
            )
            if needs_snapshot:
                snapshot = self._query_valuation_snapshot(symbol)
                if snapshot is not None and (
                    valuation_quote_error(quote) is not None or snapshot.timestamp >= quote.timestamp
                ):
                    quote = snapshot
            error = valuation_quote_error(quote)
            if error is not None:
                errors[symbol] = error
                continue
            assert quote is not None
            selected = quote.model_copy(deep=True)
            selected.extra["valuation_reference"] = (
                symbol not in open_symbols or self._fresh_valuation_quote_error(selected) is not None
            )
            quotes[symbol] = selected
        return quotes, errors

    def _query_valuation_snapshot(self, symbol):
        """从交易柜台查询单合约快照；失败时保留已有估值行情。"""
        validate_instrument(symbol)
        instrument = self._instruments[symbol]
        exchange_id = str(getattr(instrument, "ExchangeID", "") or "")
        try:
            rows = self._query(
                "ReqQryDepthMarketData",
                build_query_depth_market_data(symbol, exchange_id),
                symbol=symbol,
                timeout=min(self._timeout, 3.0),
            )
            for row in reversed(rows):
                quote = quote_to_unified(row)
                if quote.symbol != symbol or valuation_quote_error(quote) is not None:
                    continue
                self._normalize_quote_time(quote)
                quote.extra["received_at"] = time.time()
                quote.extra["valuation_source"] = "ctp_snapshot_last_price"
                with self._lock:
                    previous = self._valuation_quotes.get(symbol)
                    if previous is None or quote.timestamp >= previous.timestamp:
                        self._valuation_quotes[symbol] = quote
                return quote
        except CtpSessionRecoveryRequired:
            raise
        except Exception as exc:  # noqa: BLE001 - 快照是估值兜底，查询失败不影响既有持仓事实
            self.logger.warning("CTP 持仓行情快照查询失败: symbol={} error={}", symbol, exc)
        return None

    @override
    def get_market_data(self, symbols):
        self.initialize_websocket(symbols)
        deadline = clock_monotonic() + self._timeout
        while clock_monotonic() < deadline:
            with self._lock:
                self._require_session_ready()
                errors = {x: self._subscription_errors[x] for x in symbols if x in self._subscription_errors}
                if errors:
                    raise CtpRequestError(f"行情订阅失败: {errors}")
                if all(x in self._subscription_acks and self._quote_error(x) is None for x in symbols):
                    return {x: self._quotes[x] for x in symbols}
            get_default_clock().sleep(0.05)
        with self._lock:
            diagnostics = {symbol: self._quote_diagnostic(symbol) for symbol in symbols}
        raise TimeoutError(f"行情等待超时: {symbols}; {diagnostics}")

    def _quote_diagnostic(self, symbol):
        quote = self._quotes.get(symbol)
        return {
            "reason": self._quote_error(symbol),
            "time": self._quote_time_evidence(quote, clock_now().timestamp()) if quote else None,
            "last_rejection": self._quote_rejections.get(symbol),
            "subscription_acknowledged": symbol in self._subscription_acks,
            "subscription_error": self._subscription_errors.get(symbol),
            "samples": self._quote_diagnostics.snapshot(symbol)
            if hasattr(self, "_quote_diagnostics")
            else {"sampled": False},
        }

    @staticmethod
    def _quote_time_evidence(quote, now):
        age = now - quote.timestamp / 1000
        return {
            **quote.extra,
            "timestamp": quote.timestamp,
            "update_time": quote.update_time,
            "age_seconds": age,
            "time_direction": "unknown" if quote.timestamp <= 0 else "future" if age < 0 else "past",
        }

    def _quote_error(self, symbol):
        return self._snapshot_quote_error(symbol, self._quotes.get(symbol))

    def _fresh_valuation_quote_error(self, quote):
        return fresh_valuation_quote_error(
            quote,
            now=time.time(),
            trading_day=self._trading_day,
            max_age=self._config().quote_max_age_seconds,
        )

    def _snapshot_quote_error(self, symbol, quote, *, now=None):
        return quote_error(
            quote,
            now=time.time() if now is None else now,
            trading_day=self._trading_day,
            max_age=self._config().quote_max_age_seconds,
            tick=self.get_tick_size(symbol),
        )

    def _validate_order_quote(self, symbol, price, native_price_type):
        error = self._quote_error(symbol)
        if error is not None:
            raise CtpRequestError(f"CTP 行情不可用于新单: {symbol}: {error}")
        quote = self._quotes[symbol]
        if native_price_type == td.THOST_FTDC_OPT_LimitPrice and not price_in_bounds(
            price,
            tick=self.get_tick_size(symbol),
            lower=quote.extra["lower_limit_price"],
            upper=quote.extra["upper_limit_price"],
        ):
            raise ValueError(f"限价 {price} 不符合 tick 或涨跌停")

    def _new_ref(self):
        with self._lock:
            r = str(self._order_ref)
            self._order_ref += 1
            return r

    def plan_close_orders(
        self, symbol: str, direction: OrderDirection, volume: float, account_assets: UnifiedAccountAssets
    ) -> list[tuple[float, dict[str, object]]]:
        """使用渠道交易所信息与持仓快照拆分平今、平昨订单。"""
        exchange = str(getattr(self._instruments.get(symbol), "ExchangeID", ""))
        return self._plan_futures_close(symbol, direction, volume, account_assets, exchange)

    @override
    def _place_order_impl(self, symbol, direction, order_type, volume, price=0, **kwargs):
        validate_instrument(symbol)
        direction, order_type = normalize_order_enums(direction, order_type)
        for field in ("offset_flag", "offset"):
            if field in kwargs and kwargs[field] is None:
                raise CTPInputValidationError(f"开平标志 {field} 不能为 None")
        with self._lock:
            self._require_new_order_ready(symbol)
        self._ensure_instrument_symbols([symbol])
        if symbol not in self._instruments:
            raise ValueError(f"未知 CTP 合约: {symbol}")
        trade_rule = kwargs.get("trade_rule")
        if trade_rule is not None and not isinstance(trade_rule, dict):
            raise ValueError("trade_rule 必须是字典")
        volume = ensure_order_volume_allowed(
            volume,
            instrument=self._instruments.get(symbol),
            order_type=order_type,
            trade_rule=trade_rule if isinstance(trade_rule, dict) else None,
        )
        native_price_type = td.THOST_FTDC_OPT_LimitPrice if order_type == OrderType.LIMIT else ""
        self._validate_order_quote(symbol, price, native_price_type)
        raw = kwargs.get("offset_flag", kwargs.get("offset"))
        if raw is None:
            raw = "open"
            if is_close_intent(direction, kwargs.get("position_side")):
                raw = single_close_offset(self.plan_close_orders(symbol, direction, volume, self.get_account_assets()))
        offset = resolve_offset(raw)
        reason_code = self._get_ctp_session_block_reason(symbol)
        if reason_code is not None:
            raise AccountControlBlockedError(
                reason_code,
                execution_error=_session_block_message(reason_code),
                account_id=None,
                execution_id=None,
                channel=TradeChannel.CTP,
                operation="place_order",
                symbol=symbol,
            )
        c = self._config()
        ref = self._new_ref()
        r = build_order_insert(
            c,
            symbol=symbol,
            order_ref=ref,
            direction=direction,
            order_type=order_type,
            volume=int(volume),
            price=float(price),
            offset=offset,
        )
        oid = stable_order_id(self._trading_day, self._front_id, self._session_id, ref)
        key = {
            "order_ref": ref,
            "front_id": self._front_id,
            "session_id": self._session_id,
            "exchange_id": "",
            "order_sys_id": "",
        }
        self._order_keys[oid] = key
        try:
            self._call_trader_request("ReqOrderInsert", r)
        except Exception:
            self._order_keys.pop(oid, None)
            raise
        return UnifiedOrder.create(
            order_id=oid,
            symbol=symbol,
            direction=direction.value,
            order_type=order_type.value,
            volume=volume,
            price=price,
            channel_type=self.channel_type,
            status=OrderStatus.SUBMITTED,
            offset_flag=offset,
            **key,
        )

    def _on_order(self, row):
        with self._lock:
            if self._closed or self._invalid_reason:
                return
            try:
                o = self._remember_order(row)
            except CtpRequestError as exc:
                self._invalidate_connection(str(exc))
                return
        # 订单回调可能同步触发撤单后的补单；先交付成交并解除待关联门禁，
        # 使跟踪器按完整成交量计算剩余量，再允许订单终态驱动下一步。
        self._replay_unassociated_trades()
        self._control_order_result(
            row, "broker_rejected" if o.status == OrderStatus.REJECTED else "broker_acknowledged", order_id=o.order_id
        )
        self.account_orders.apply(o)
        self._dispatch(self._order_callbacks, o)

    def _on_trade(self, row):
        with self._lock:
            if self._closed or self._invalid_reason:
                return
            if not self._ready:
                self._startup_trades.append(_copy_native_row(row))
                return
            try:
                trade = self._convert_trade(row)
            except _TradeAssociationPending as exc:
                # 保留原始帧并阻断新单，允许后到报单补关联。
                self.logger.error(str(exc))
                return
            except CtpRequestError as exc:
                self._invalidate_connection(str(exc))
                return
        self._dispatch_trade_once(row, trade)

    def _native_trade_key(self, row):
        """交易日与交易所隔离成交身份；无 TradeID 时保留完整原生证据。"""
        day = str(getattr(row, "TradingDay", "") or self._trading_day)
        exchange = str(getattr(row, "ExchangeID", ""))
        trade_id = str(getattr(row, "TradeID", "")).strip()
        if trade_id:
            return (day, exchange, trade_id)
        return (day, exchange, repr(sorted(vars(_copy_native_row(row)).items())))

    def _dispatch_trade_once(self, row, trade):
        with self._lock:
            key = self._native_trade_key(row)
            self._unassociated_trades.pop(key, None)
            if key in self._dispatched_trades:
                return
            self._dispatched_trades.add(key)
        self.account_orders.activity.observe_trade(trade)
        self._dispatch(self._trade_callbacks, trade)

    def _replay_unassociated_trades(self):
        with self._lock:
            rows = list(self._unassociated_trades.values())
        for row in rows:
            try:
                trade = self._convert_trade(row)
            except _TradeAssociationPending:
                continue
            except CtpRequestError as exc:
                self._invalidate_connection(str(exc))
                return
            self._dispatch_trade_once(row, trade)

    def _log_error(self, info, name):
        e = self._error(info, name)
        if e:
            self.logger.error(str(e))

    def _on_order_insert_error(self, row, info, *, source: str) -> None:
        """把异步报单拒绝关联到稳定订单身份，并推进 REJECTED 终态。"""
        detail = insert_error_detail(info)
        if detail is None:
            return
        error_id, error_msg = detail
        with self._lock:
            if self._closed or self._invalid_reason:
                return
            order_id = resolve_insert_reject_order_id(
                row=row,
                order_keys=self._order_keys,
                trading_day=self._trading_day,
                front_id=self._front_id,
                session_id=self._session_id,
                stable_order_id=stable_order_id,
            )
            if not order_id:
                self.logger.error(f"报单拒绝无法关联订单: ErrorID={error_id}, {error_msg}, source={source}")
                return
            self._control_order_result(
                row, "broker_rejected", {"error_id": error_id, "error_msg": error_msg}, order_id=order_id
            )
            key = dict(self._order_keys.get(order_id, {}))
            symbol = str(getattr(row, "InstrumentID", "") or key.get("symbol", "") or "")
            if not symbol:
                # 从已登记订单键无法拿 symbol 时，尽量保留可诊断信息。
                symbol = str(getattr(row, "InstrumentID", "") or "")
            direction = getattr(row, "Direction", None)
            if direction == td.THOST_FTDC_D_Sell:
                direction_value = OrderDirection.SELL.value
            else:
                direction_value = OrderDirection.BUY.value
            price_type = getattr(row, "OrderPriceType", None)
            order_type_value = (
                OrderType.LIMIT.value if price_type == td.THOST_FTDC_OPT_LimitPrice else OrderType.MARKET.value
            )
            try:
                volume = float(getattr(row, "VolumeTotalOriginal", 0) or 0)
            except (TypeError, ValueError):
                volume = 0.0
            try:
                price = float(getattr(row, "LimitPrice", 0) or 0)
            except (TypeError, ValueError):
                price = 0.0
            offset = str(getattr(row, "CombOffsetFlag", key.get("offset_flag", "")) or "")
            fields = rejected_order_update(
                order_id=order_id,
                symbol=symbol,
                direction=direction_value,
                order_type=order_type_value,
                volume=volume,
                price=price,
                channel_type=self.channel_type,
                offset_flag=offset,
                error_id=error_id,
                error_msg=error_msg,
                source=source,
                extra={
                    "order_ref": key.get("order_ref", str(getattr(row, "OrderRef", "") or "")),
                    "front_id": key.get("front_id", getattr(row, "FrontID", self._front_id)),
                    "session_id": key.get("session_id", getattr(row, "SessionID", self._session_id)),
                    "exchange_id": key.get("exchange_id", str(getattr(row, "ExchangeID", "") or "")),
                    "order_sys_id": key.get("order_sys_id", ""),
                },
            )
            rejected = UnifiedOrder.create(**fields)
            self.logger.error(f"报单拒绝已关联订单 {order_id}: ErrorID={error_id}, {error_msg}, source={source}")
        self.account_orders.apply(rejected)
        self._dispatch(self._order_callbacks, rejected)

    def reconcile_terminal_order(self, symbol: str, order_id: str):
        """查询单订单终态；查不到时返回 None，不把缺失当成已撤。"""
        validate_instrument(symbol)
        r = build_query_orders(self._config(), symbol)
        matched = None
        for row in self._query("ReqQryOrder", r):
            order = self._remember_order(row)
            if order.order_id == order_id:
                matched = order
                break
        if matched is None:
            self.logger.info(f"单订单对账未找到 {order_id}，保持未知/待对账，不伪造成撤单")
            return None
        if matched.is_completed():
            return matched
        return None

    @override
    def _cancel_order_impl(self, symbol, order_id):
        validate_instrument(symbol)
        validate_cancel_id(order_id)
        key = self._order_keys.get(order_id)
        if not key:
            self._get_pending_orders_impl(symbol)
            key = self._order_keys.get(order_id)
        if not key:
            raise ValueError(f"找不到订单撤单键: {order_id}")
        r = build_order_cancel(self._config(), symbol=symbol, key=key)
        self._send_trader_request("cancel_order_ctp", "ReqOrderAction", r, symbol=symbol)
        return True

    @override
    def _get_pending_orders_impl(self, symbol=None):
        if symbol is not None:
            validate_instrument(symbol, allow_all=True)
        r = build_query_orders(self._config(), symbol)
        orders = [self._remember_order(x) for x in self._query("ReqQryOrder", r)]
        self._replay_unassociated_trades()
        return [o for o in orders if o.is_active() and (not symbol or o.symbol == symbol)]

    @override
    def _query_trades_impl(self, symbol, order_id):
        validate_instrument(symbol, allow_all=True)
        r = build_query_trades(self._config(), symbol)
        ts = [self._convert_trade(x) for x in self._query("ReqQryTrade", r)]
        return [x for x in ts if x.order_id == order_id]

    def _query_recent_orders_impl(self, symbol=None):
        """查询当前交易日柜台委托，包含真实终态。"""
        if symbol is not None:
            validate_instrument(symbol, allow_all=True)
        rows = self._query("ReqQryOrder", build_query_orders(self._config(), symbol))
        return [self._remember_order(row) for row in rows]

    def _query_recent_trades_impl(self, symbol=None):
        """查询当前交易日柜台逐笔成交，沿用完整订单身份关联。"""
        if symbol is not None:
            validate_instrument(symbol, allow_all=True)
        rows = self._query("ReqQryTrade", build_query_trades(self._config(), symbol or ""))
        return [self._convert_trade(row) for row in rows]

    @override
    def get_tick_size(self, symbol):
        if symbol not in self._instruments:
            self._ensure_instrument_symbols([symbol])
        x = self._instruments.get(symbol)
        v = float(getattr(x, "PriceTick", 0) or 0) if x else 0
        return v if v > 0 else None

    def get_order_volume_bounds(self, symbol, order_type=OrderType.LIMIT, trade_rule=None):
        """返回当前合约与用户规则合并后的有效最小量和最大量。"""
        if trade_rule is not None and not isinstance(trade_rule, dict):
            raise ValueError("trade_rule 必须是字典")
        return effective_max_order_volume(
            self._instruments.get(symbol),
            order_type,
            trade_rule if isinstance(trade_rule, dict) else None,
        )

    def get_max_order_volume(self, symbol, order_type=OrderType.LIMIT, trade_rule=None):
        """兼容仅查询上限的调用方。"""
        return self.get_order_volume_bounds(symbol, order_type, trade_rule)[1]

    @override
    def _calculate_generic_sizing(
        self,
        weight: float,
        price: float,
        account_assets: UnifiedAccountAssets,
        trade_rule: dict[str, object],
        *,
        symbol: str | None = None,
    ) -> TargetSizingDecision:
        """按 CTP 合约规格生成目标手数及换算证据."""
        sizing_mode = str(trade_rule.get("sizing_mode", "weight"))
        if sizing_mode == "lots":
            target = self._round_lots(weight, trade_rule)
            step = self._quantity_step(trade_rule)
            return TargetSizingDecision(
                symbol=symbol or "",
                sizing_mode=sizing_mode,
                reason_code=("COMMON.SIZING.EXACT" if abs(target - weight) <= 1e-12 else "COMMON.SIZING.QUANTIZED"),
                account_weight=weight,
                equity=account_assets.total_asset,
                raw_quantity=weight,
                target_quantity=target,
                quantity_step=step,
                min_quantity=step,
            )
        # 通用规划只传入价格标量，无法证明原快照的新鲜度；在此显式选取
        # 最新快照，校验和定量始终使用同一个对象，避免回调更新造成价格竞态。
        quote = self._quotes.get(symbol) if symbol else None
        invalid_quote = bool(symbol) and self._snapshot_quote_error(symbol, quote) is not None
        if quote is not None:
            price = quote.last_price
        if invalid_quote or not 0 < price < 1.7976931348623157e308:
            return TargetSizingDecision(
                symbol=symbol or "",
                status="UNAVAILABLE",
                reason_code="COMMON.SIZING.INVALID_PRICE",
                account_weight=weight,
                equity=account_assets.total_asset,
                reference_price=price,
            )
        multiplier = self._resolve_volume_multiple(symbol, trade_rule)
        if multiplier <= 0:
            return TargetSizingDecision(
                symbol=symbol or "",
                status="UNAVAILABLE",
                reason_code="COMMON.SIZING.MISSING_UNIT_MULTIPLIER",
                account_weight=weight,
                equity=account_assets.total_asset,
                reference_price=price,
            )
        raw_quantity = account_assets.total_asset * weight / (price * multiplier)
        target_quantity = self._round_lots(raw_quantity, trade_rule)
        reason = "COMMON.SIZING.EXACT"
        if raw_quantity != 0 and target_quantity == 0:
            reason = "COMMON.SIZING.BELOW_MIN_QUANTITY"
        elif abs(raw_quantity - target_quantity) > 1e-12:
            reason = "COMMON.SIZING.QUANTIZED"
        step = self._quantity_step(trade_rule)
        return TargetSizingDecision(
            symbol=symbol or "",
            sizing_mode=sizing_mode,
            reason_code=reason,
            account_weight=weight,
            equity=account_assets.total_asset,
            reference_price=price,
            unit_multiplier=float(multiplier),
            unit_notional=price * multiplier,
            target_notional=abs(account_assets.total_asset * weight),
            raw_quantity=raw_quantity,
            target_quantity=target_quantity,
            quantity_step=step,
            min_quantity=step,
        )

    def _resolve_volume_multiple(self, symbol: str | None, trade_rule: dict[str, object]) -> int:
        instrument = self._instruments.get(symbol) if symbol else None
        native = getattr(instrument, "VolumeMultiple", 0) if instrument is not None else 0
        if isinstance(native, int | float) and not isinstance(native, bool) and native > 0:
            return int(native)
        fallback = trade_rule.get("contract_multiplier")
        if isinstance(fallback, int | float) and not isinstance(fallback, bool) and fallback > 0:
            return int(fallback)
        return 0

    @staticmethod
    def _round_lots(volume: float, trade_rule: dict[str, object]) -> float:
        precision = trade_rule.get("quantity_precision")
        if isinstance(precision, int) and not isinstance(precision, bool):
            return round(volume, precision)
        minimum = trade_rule.get("最小交易单位", 1)
        lot = float(minimum) if isinstance(minimum, int | float) and not isinstance(minimum, bool) else 1.0
        if lot > 1:
            sign = 1.0 if volume >= 0 else -1.0
            return sign * (int(abs(volume) / lot) * lot)
        return float(int(volume)) if volume >= 0 else float(-int(-volume))

    @staticmethod
    def _quantity_step(trade_rule: dict[str, object]) -> float:
        precision = trade_rule.get("quantity_precision")
        if isinstance(precision, int) and not isinstance(precision, bool):
            return 10 ** (-precision)
        minimum = trade_rule.get("最小交易单位", 1)
        return float(minimum) if isinstance(minimum, int | float) and not isinstance(minimum, bool) else 1.0

    def _on_quote(self, row):
        with self._lock:
            if self._closed or self._invalid_reason:
                return
            received_at = clock_now().timestamp()
            q = quote_to_unified(row)
            if q.symbol not in self._subscriptions:
                return
            if q.extra.get("trading_day") != self._trading_day:
                self._invalidate_connection(
                    "行情交易日变化，请重建 CTP 会话",
                    connection_failure=True,
                    label="交易日已变化",
                    presentation=lambda: self._session_problem(
                        "交易日已变化",
                        "已订阅行情的交易日发生变化，当前会话已失效。",
                        {"会话交易日": self._trading_day, "行情交易日": q.extra.get("trading_day")},
                    ),
                )
                return
            self._normalize_quote_time(q)
            q.extra["received_at"] = received_at
            error = self._snapshot_quote_error(q.symbol, q, now=received_at)
            previous = self._quotes.get(q.symbol)
            out_of_order = previous is not None and q.timestamp <= previous.timestamp <= received_at * 1000
            if not hasattr(self, "_quote_diagnostics"):
                self._quote_diagnostics = QuoteDiagnostics()
            self._quote_diagnostics.observe(
                q.symbol,
                event_time_ms=q.timestamp,
                received_at=received_at,
                reason=error or ("out_of_order" if out_of_order else None),
            )
            valuation_previous = self._valuation_quotes.get(q.symbol)
            if (
                not out_of_order
                and valuation_quote_error(q) is None
                and (valuation_previous is None or q.timestamp > valuation_previous.timestamp)
            ):
                self._valuation_quotes[q.symbol] = q
            if error or out_of_order:
                self._quote_rejections[q.symbol] = {
                    "reason": error or "out_of_order",
                    **self._quote_time_evidence(q, received_at),
                }
            if out_of_order or (
                error
                and previous is not None
                and self._snapshot_quote_error(q.symbol, previous, now=received_at) is None
            ):
                return
            self._quotes[q.symbol] = q
        self._dispatch(self._price_callbacks, q)

    def _normalize_quote_time(self, quote):
        instrument = self._instruments.get(quote.symbol)
        sessions = ()
        if getattr(instrument, "ProductClass", None) == td.THOST_FTDC_PC_Futures:
            sessions = get_ctp_product_sessions(
                getattr(instrument, "ExchangeID", ""), getattr(instrument, "ProductID", "")
            )
        calendar = getattr(self, "_trading_calendar", None)
        calendar_id = getattr(self, "_channel_calendar_id", None) or CHINA_CALENDAR_ID
        if calendar is None:
            calendar = self._quote_calendar
            calendar_id = CHINA_CALENDAR_ID
        value, status, reason = resolve_quote_time(
            quote.update_time, self._trading_day, sessions, lambda day: calendar.is_open(calendar_id, day)
        )
        quote.update_time = value
        quote.timestamp = int(datetime.fromisoformat(value).timestamp() * 1000) if value else 0
        quote.extra.update(
            event_time_status=status, normalized_date=value[:10] if value else "", event_time_reason=reason
        )
        key = (quote.symbol, status)
        if status != "native" and key not in self._quote_time_logged:
            self._quote_time_logged.add(key)
            self.logger.info(
                f"CTP 行情时间: symbol={quote.symbol} status={status} reason={reason} "
                f"original={quote.extra.get('event_date')} normalized={value}"
            )

    def _market_subscribed(self, row, info):
        """保留订阅失败，禁止以缓存行情掩盖异步拒绝。"""
        with self._lock:
            if self._closed or self._invalid_reason:
                return
            error = self._error(info, "行情订阅")
            symbol = str(getattr(row, "InstrumentID", ""))
            if error:
                if not symbol:
                    self._invalidate_connection(error)
                    return
                self._subscription_errors[symbol] = str(error)
                self._quotes.pop(symbol, None)
            elif symbol in self._subscriptions:
                self._subscription_acks.add(symbol)

    @override
    def initialize_websocket(self, symbols=None):
        if not symbols:
            return
        symbols = list(symbols)
        for symbol in symbols:
            validate_instrument(symbol)
        self._ensure_instrument_symbols(list(symbols))
        unknown = [x for x in symbols if x not in self._instruments]
        if unknown:
            raise ValueError(f"未知 CTP 合约: {unknown}")
        with self._lock:
            self._require_session_ready()
            new_symbols = [symbol for symbol in dict.fromkeys(symbols) if symbol not in self._subscriptions]
            if not new_symbols:
                return
            encoded = [symbol.encode() for symbol in new_symbols]
            self._subscriptions.update(new_symbols)
            try:
                self._check(self._market_api.SubscribeMarketData(encoded, len(encoded)), "行情订阅")
            except Exception:
                self._subscriptions.difference_update(new_symbols)
                self._subscription_acks.difference_update(new_symbols)
                for symbol in new_symbols:
                    self._quotes.pop(symbol, None)
                raise
            self._monitoring = True

    def _dispatch(self, callbacks, value):
        for cb in list(callbacks):
            try:
                cb(value)
            except Exception:
                self.logger.exception("CTP callback 执行失败")

    def _register(self, items, cb):
        with self._lock:
            if cb not in items:
                items.append(cb)

    def _unregister(self, items, cb):
        with self._lock:
            if cb in items:
                items.remove(cb)

    def register_order_callback(self, cb):
        """注册订单回调。"""
        self._register(self._order_callbacks, cb)

    def register_trade_callback(self, cb):
        """注册成交回调。"""
        self._register(self._trade_callbacks, cb)

    def register_price_callback(self, cb):
        """注册行情回调。"""
        self._register(self._price_callbacks, cb)

    def unregister_order_callback(self, cb):
        """注销订单回调。"""
        self._unregister(self._order_callbacks, cb)

    def unregister_trade_callback(self, cb):
        """注销成交回调。"""
        self._unregister(self._trade_callbacks, cb)

    def unregister_price_callback(self, cb):
        """注销行情回调。"""
        self._unregister(self._price_callbacks, cb)

    def is_monitoring(self):
        """返回行情监控状态。"""
        with self._lock:
            return (
                self._monitoring
                and self._verify_connection()
                and not self._subscription_errors
                and all(symbol in self._quotes and symbol in self._subscription_acks for symbol in self._subscriptions)
            )

    def submit_option_action(self, symbol, action, volume, **kwargs):
        """提交期权指令。"""
        if symbol not in self._instruments:
            raise ValueError(f"未知 CTP 合约: {symbol}")
        if not isinstance(volume, int) or volume <= 0:
            raise ValueError("期权指令数量必须为正整数")
        c = self._config()
        ref = self._new_ref()
        kind = OptionActionType(action)
        record = OptionActionRecord(ref, symbol, kind, volume, submit_time=datetime.now().isoformat())
        req, name = build_option_insert(
            broker_id=c.broker_id,
            investor_id=c.investor_id,
            order_ref=ref,
            instrument_id=symbol,
            action=kind,
            volume=volume,
        )
        self._option_actions[ref] = record
        try:
            operation = {
                OptionActionType.EXERCISE: "option_exercise",
                OptionActionType.ABANDON: "option_abandon",
                OptionActionType.SELF_CLOSE: "option_self_close",
            }[kind]
            self._send_trader_request(operation, name, req, symbol=symbol)
        except Exception:
            self._option_actions.pop(ref, None)
            raise
        return record

    def cancel_option_action(self, ref):
        """撤销期权指令。"""
        record = self._option_actions.get(ref)
        if not record:
            raise KeyError(f"未知期权指令: {ref}")
        c = self._config()
        req, name = build_option_cancel(record, broker_id=c.broker_id, investor_id=c.investor_id)
        operation = {
            OptionActionType.EXERCISE: "cancel_option_exercise",
            OptionActionType.ABANDON: "cancel_option_abandon",
            OptionActionType.SELF_CLOSE: "cancel_option_self_close",
        }[record.action]
        self._send_trader_request(operation, name, req, symbol=record.instrument_id)
        record.status = OptionActionStatus.CANCELLING
        return True

    def get_option_action_status(self, ref):
        """返回当前进程中的期权指令状态。"""
        return self._option_actions.get(ref)

    def _option_response(self, row, info):
        ref = option_ref(row)
        r = self._option_actions.get(ref)
        if r:
            if self._error(info, "期权指令"):
                self._option_actions[ref] = fail_option_action(r, info, "front")
            else:
                self._option_actions[ref] = accept_option_action(r)

    def _option_error(self, row, info):
        ref = option_ref(row)
        r = self._option_actions.get(ref)
        if r:
            self._option_actions[ref] = fail_option_action(r, info, "exchange")

    def _option_return(self, row):
        ref = option_ref(row)
        r = self._option_actions.get(ref)
        if not r:
            return
        self._option_actions[ref] = finish_option_action(r, row)

    def _option_cancel_response(self, row, info):
        ref = option_ref(row)
        r = self._option_actions.get(ref)
        if r and self._error(info, "期权撤销"):
            self._option_actions[ref] = fail_option_action(r, info, "front")

    def is_exercise_valuable(self, symbol):
        """根据标的现价和行权价判断内在价值。"""
        x = self._instruments.get(symbol)
        if not x:
            raise ValueError(f"未知 CTP 合约: {symbol}")
        u = str(x.UnderlyingInstrID)
        prices = self.get_market_data([symbol, u])
        return (
            prices[u].last_price > float(x.StrikePrice)
            if x.OptionsType == td.THOST_FTDC_CP_CallOptions
            else prices[u].last_price < float(x.StrikePrice)
        )

    @override
    def _get_account_mark(self):
        c = self._config()
        return f"ctp_{c.broker_id}_{c.investor_id}"

    @override
    def _get_default_trade_rules_for_empty(self, symbols):
        return {x: {"price": "PASSIVE", "max_single_order_size": 50} for x in symbols}

    @override
    def _cleanup(self):
        self.close()

    def close(self):
        """按行情、交易顺序幂等释放 API。"""
        connection_monitor(self).close()
        self._instrument_provider.close()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._ready = False
        self._monitoring = False
        self._fail_waiters(ConnectionError("CTPExecutor 已关闭"))
        for name in ("_market_api", "_trader_api"):
            api = getattr(self, name)
            if api:
                try:
                    api.RegisterSpi(None)
                    api.Release()
                    self.logger.info("CTP API 已释放: {}", name)
                except Exception:
                    self.logger.exception("释放 OpenCTP API 失败")
                setattr(self, name, None)
        self._market_connected = self._trader_connected = False
        if self._flow_dir:
            shutil.rmtree(self._flow_dir, ignore_errors=True)
            self._flow_dir = None

    @override
    def stop(self):
        self.close()


__all__ = ["CTPExecutor"]
