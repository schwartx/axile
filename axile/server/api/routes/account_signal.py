"""调仓信号数据测试、配置读取与发布。"""

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from msgspec import Meta

from axile.channels.market_data import MarketDataUnavailable
from axile.channels.rebalance_signal import (
    SIGNAL_CHANNEL,
    SignalAccountConfig,
    SignalAccountContext,
    SignalQuoteSource,
)
from axile.common.default_account_notification import build_signal_feishu_card
from axile.common.feishu import push_feishu_card
from axile.common.gm_symbols import GM_SYMBOL_RESOLVER
from axile.common.notification_function import run_notification_function
from axile.common.serialization import parse_input, to_payload
from axile.executor.algorithms.utils.clock import clock_now, clock_now_iso
from axile.executor.models.unified_price import UnifiedPriceData
from axile.server._native import _core
from axile.server.api.inputs import json_body
from axile.server.api.routes.account_support import _get_account_or_404, ensure_account_current
from axile.server.error_details import exception_details
from axile.server.native_store import native_operation
from axile.server.signal_runner import run_signal_account_test, run_signal_quote_test
from axile.server.signal_service import publish_account_signal, signal_notification_sample
from axile.server.tushare_quotes import read_tushare_quotes

router = APIRouter()
QUOTE_TEST_TIMEOUT = 5


@dataclass(kw_only=True)
class SignalQuoteTestRequest:
    """用当前行情来源的草稿测试一组标的。"""

    quote_source: SignalQuoteSource
    symbols: Annotated[list[Annotated[str, Meta(min_length=1, max_length=80)]], Meta(min_length=1, max_length=10)]

    __forbid_unknown__ = True


@dataclass(kw_only=True)
class SignalNotificationTestRequest:
    """用尚未保存的通知草稿测试一个市场的样例信号。"""

    mode: Literal["feishu", "function"]
    draft: str
    name: str
    market: str

    __forbid_unknown__ = True


@dataclass(kw_only=True)
class SignalAccountTestRequest:
    """用当前开户资料试跑未保存的账户函数。"""

    code: str
    name: str = ""
    market: str = ""

    __forbid_unknown__ = True

    def validate(self) -> None:
        """要求草稿包含账户函数。"""
        if not self.code.strip():
            raise ValueError("请填写账户函数")


def _read_gm_test_quotes(
    source: SignalQuoteSource, symbols: list[str], errors: dict[str, str]
) -> dict[str, UnifiedPriceData]:
    for symbol in symbols:
        try:
            GM_SYMBOL_RESOLVER.to_gm(symbol)
        except ValueError as exc:
            errors[symbol] = str(exc)
    valid = [symbol for symbol in symbols if symbol not in errors]
    if valid:
        with closing(source.gm_config().open()) as market:
            try:
                return market.snapshot(valid, timeout=QUOTE_TEST_TIMEOUT, allow_partial=True)
            except (TimeoutError, MarketDataUnavailable) as exc:
                errors.update({symbol: str(exc) for symbol in valid})
    return {}


def _test_quotes(payload: SignalQuoteTestRequest) -> dict[str, Any]:
    source = payload.quote_source
    if source.kind in ("gm", "tushare"):
        symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in payload.symbols))
    else:
        symbols = list(dict.fromkeys(symbol.strip() for symbol in payload.symbols))
    errors: dict[str, str] = {}
    if source.kind == "gm":
        quotes = _read_gm_test_quotes(source, symbols, errors)
        missing = "掘金未返回该标的行情"
    elif source.kind == "tushare":
        quotes = read_tushare_quotes(source.url, source.token, symbols, QUOTE_TEST_TIMEOUT, allow_partial=True)
        missing = "Tushare 未返回该标的行情"
    elif source.kind in ("ctp", "tq"):
        with closing(source.futures_config().open()) as market:
            quotes = market.snapshot(symbols, timeout=QUOTE_TEST_TIMEOUT)
        missing = "未返回该合约行情"
    else:
        quotes = run_signal_quote_test(source.code, symbols, QUOTE_TEST_TIMEOUT)
        missing = "行情函数未返回该标的行情"
    return {
        "items": [
            {
                "symbol": symbol,
                "quote": {
                    "last_price": quote.last_price,
                    "timestamp": quote.timestamp,
                    "bids": quote.bids,
                    "asks": quote.asks,
                }
                if (quote := quotes.get(symbol)) is not None
                else None,
                "error": None if quote is not None else errors.get(symbol, missing),
            }
            for symbol in symbols
        ],
    }


@router.post("/signal/quote-test", response_model=None)
async def test_signal_quotes(
    payload: Annotated[SignalQuoteTestRequest, Depends(json_body(SignalQuoteTestRequest))],
) -> dict[str, Any]:
    """用当前草稿查询一次行情。"""
    try:
        return await asyncio.to_thread(_test_quotes, payload)
    except (ValueError, TimeoutError, MarketDataUnavailable) as exc:
        raise HTTPException(422, detail=exception_details(exc, "行情测试失败")) from exc


@router.post("/signal/account-test", response_model=None)
async def test_signal_account(
    payload: Annotated[SignalAccountTestRequest, Depends(json_body(SignalAccountTestRequest))],
) -> dict[str, Any]:
    """执行账户函数并返回校验后的账户快照。"""
    context: SignalAccountContext = {"account_id": None, "name": payload.name, "market": payload.market}
    try:
        snapshot = await asyncio.to_thread(run_signal_account_test, payload.code, context, 5)
    except (ValueError, TimeoutError) as exc:
        raise HTTPException(422, detail=exception_details(exc, "账户函数测试失败")) from exc
    return to_payload(snapshot, mode="json")


@router.post("/signal/notification-test", response_model=None)
async def test_signal_notification(
    payload: Annotated[SignalNotificationTestRequest, Depends(json_body(SignalNotificationTestRequest))],
) -> dict[str, Any]:
    """发送样例卡片或试跑通知函数，不依赖已创建的账户。"""
    name = payload.name.strip() or "样例账户"
    draft = payload.draft.strip()
    if not draft:
        raise HTTPException(422, detail="请填写飞书 Webhook 或通知函数")
    at = clock_now_iso()
    try:
        context, targets, unit = signal_notification_sample(name, payload.market, at)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if payload.mode == "feishu":
        card = build_signal_feishu_card(name, targets, unit, market=payload.market, at=at, is_test=True)
        try:
            await asyncio.to_thread(push_feishu_card, card, draft)
        except (RuntimeError, ValueError) as exc:
            return {"ok": False, "message": str(exc).replace(draft, "[已隐藏]")}
        return {"ok": True, "message": "测试卡片已发送，请到飞书群确认。"}
    result = await asyncio.to_thread(run_notification_function, payload.draft, context)
    return {
        "ok": result.ok,
        "message": "函数已试跑完成。" if result.ok else result.error or "运行失败",
        "error_line": result.error_line,
        "traceback": result.traceback,
    }


@router.get("/{account_id}/signal-config", response_model=None)
async def signal_config(account_id: int) -> dict[str, Any]:
    """读取调仓信号编辑页需要的函数和手动快照配置。"""
    account = await _get_account_or_404(account_id)
    if account.trade_channel != SIGNAL_CHANNEL:
        raise HTTPException(404, detail="该账户不是调仓信号账户")
    config = parse_input(SignalAccountConfig, account.account_config)
    return {"account_config": to_payload(config, mode="json", exclude={"channel_type"})}


@router.post("/{account_id}/signal/run", response_model=None)
async def run_signal_now(account_id: int) -> dict[str, str]:
    """手动生成并提交调仓信号，包括暂停定时计算的账户。"""
    account = await _get_account_or_404(account_id)
    ensure_account_current(account)
    if account.trade_channel != SIGNAL_CHANNEL:
        raise HTTPException(409, detail="该账户不支持调仓信号")
    if account.portfolio_id is None:
        raise HTTPException(409, detail="请先绑定组合")
    try:
        publication = await publish_account_signal(account)
    except _core.NativeError as exc:
        if exc.args[0] in {"TARGET_INVALID", "ACCOUNT_CONFLICT", "ARCHIVED"}:
            raise HTTPException(409, detail=exc.args[1]) from exc
        raise
    except (ValueError, TimeoutError) as exc:
        raise HTTPException(422, detail=exception_details(exc, "调仓信号执行失败")) from exc
    if publication is None:
        raise HTTPException(409, detail="账户正在计算或刷新，或者组合绑定已变更")
    return publication


@router.get("/{account_id}/signal/records", response_model=None)
async def list_signal_records(
    account_id: int,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    before: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    days: Annotated[int | None, Query(ge=1, le=365)] = None,
    status: Literal["SUCCEEDED", "FAILED"] | None = None,
    symbol: Annotated[str | None, Query(max_length=80)] = None,
    trigger_source: Literal["manual", "scheduler"] | None = None,
) -> dict[str, Any]:
    """分页读取账户已保存的调仓信号。"""
    account = await _get_account_or_404(account_id)
    if account.trade_channel != SIGNAL_CHANNEL:
        raise HTTPException(404, detail="该账户不是调仓信号账户")
    since = (clock_now() - timedelta(days=days)).isoformat() if days is not None else None
    return await native_operation(
        "signal_records",
        {
            "account_id": account_id,
            "limit": limit,
            "before": before,
            "since": since,
            "status": status,
            "symbol": (symbol or "").strip() or None,
            "trigger_source": trigger_source,
        },
    )
