"""声明公开包内置的 CTP 与 GM 渠道插件."""

from __future__ import annotations

from typing import cast

import pandas as pd

from axile.channels.algorithm_scopes import (
    FUTURES_SCOPE_PATTERN,
    GM_SCOPE_PATTERN,
    futures_algorithm_scope,
    gm_algorithm_scope,
)
from axile.channels.cn_futures import canonicalize_cn_futures_symbol, quantize_cn_futures_quantity
from axile.channels.contracts import (
    AlgorithmReference,
    ChannelAccountField,
    ChannelAccountFieldClipboard,
    ChannelAccountFieldCondition,
    ChannelAccountFieldConstraints,
    ChannelAccountForm,
    ChannelAccountNotice,
    ChannelAccountOption,
    ChannelAlgorithmScope,
    ChannelCalendar,
    ChannelConnectionValidation,
    ChannelDefaults,
    ChannelDescriptor,
    ChannelEndpointConstraints,
    ChannelLeverage,
    ChannelNightSchedule,
    ChannelNumberConstraints,
    ChannelPlugin,
    ChannelPortfolioPreset,
    ChannelReplayCapability,
    ChannelSchedule,
    ChannelSessionWindow,
    ChannelUi,
    ChannelUnits,
    ConnectionProgress,
    ConnectionStepDefinition,
    ReplayDisplayField,
)
from axile.channels.gm_sessions import GMSessionSettings
from axile.channels.schedule_clock import (
    CN_FUTURES_WINDOWS,
    CN_STOCK_WINDOWS,
    close_lead_hhmm,
    overnight_windows,
    rhythm_hhmm,
)
from axile.common.order_param_model import OrderParamModel
from axile.common.trade_channel import TradeChannel
from axile.executor.futures_close_policy import close_capability
from axile.executor.models.unified_input_accounts import (
    BaseAccountConfig,
    CTPAccountConfig,
    GMAccountConfig,
    TQAccountConfig,
)


def _ctp_replay_capability() -> ChannelReplayCapability:
    """声明原生期货观察能力，实际档数和覆盖由录制确认。"""
    offsets = {"0": "开仓", "1": "平仓", "2": "强平", "3": "平今", "4": "平昨", "5": "强减", "6": "本地强平"}
    attributes = [
        "hedge_flag",
        "native_order_type",
        "time_condition",
        "volume_condition",
        "submit_status",
        "status_msg",
        "native_direction",
        "native_status",
        "native_comb_offset_flag",
    ]
    identities = [
        "trading_day",
        "front_id",
        "session_id",
        "order_ref",
        "exchange_id",
        "order_sys_id",
        "trade_id",
        "request_id",
    ]
    return ChannelReplayCapability(
        supported=True,
        adapter_version="2",
        streams={
            kind: {
                "availability": "partial",
                "reason": "waiting_observation",
                "delivery": delivery,
                "intermediate_states": "observed_only",
            }
            for kind, delivery in (
                ("depth", "callback"),
                ("own_order", "callback"),
                ("own_trade", "callback"),
                ("position", "query"),
            )
        }
        | {"market_trade": {"availability": "unavailable", "reason": "source_not_provided"}},
        attributes={
            "order": attributes,
            "command": attributes,
            "trade": ["native_direction", "native_offset_flag"],
            "position": ["native_position_direction"],
        },
        native_identity={
            kind: identities
            for kind in ("quote", "order", "trade", "command", "account_observation", "order_set", "connection")
        }
        | {"position": ["trading_day", "exchange_id"]},
        display={
            "order": [
                ReplayDisplayField(
                    key="hedge_flag",
                    label="投机／套保",
                    kind="enum",
                    values={"1": "投机", "2": "套利", "3": "套保", "5": "做市"},
                ),
                ReplayDisplayField(key="status_msg", label="柜台说明"),
                ReplayDisplayField(
                    key="native_direction", label="原生买卖方向", kind="enum", values={"0": "买", "1": "卖"}
                ),
                ReplayDisplayField(
                    key="native_status",
                    label="原生委托状态",
                    kind="enum",
                    values={
                        "0": "全部成交",
                        "1": "部分成交，在队列中",
                        "2": "部分成交，不在队列中",
                        "3": "未成交，在队列中",
                        "4": "未成交，不在队列中",
                        "5": "已撤单",
                        "a": "未知",
                        "b": "尚未触发",
                        "c": "已触发",
                    },
                ),
                ReplayDisplayField(
                    key="native_comb_offset_flag",
                    label="原生开平标志",
                    kind="enum",
                    values=offsets,
                ),
            ],
            "trade": [
                ReplayDisplayField(
                    key="native_direction", label="原生买卖方向", kind="enum", values={"0": "买", "1": "卖"}
                ),
                ReplayDisplayField(
                    key="native_offset_flag",
                    label="原生开平标志",
                    kind="enum",
                    values=offsets,
                ),
            ],
            "position": [
                ReplayDisplayField(
                    key="native_position_direction",
                    label="原生持仓方向",
                    kind="enum",
                    values={"1": "净持仓", "2": "多头", "3": "空头"},
                )
            ],
        },
    )


def _gm_replay_capability() -> ChannelReplayCapability:
    """声明掘金实际回调与查询快照；不承诺连续行情或市场逐笔。"""
    attributes = [
        "side",
        "status",
        "order_type",
        "order_style",
        "order_business",
        "position_effect",
        "position_side",
        "ord_rej_reason",
        "ord_rej_reason_detail",
        "order_duration",
        "order_qualifier",
        "stop_price",
        "filled_amount",
        "filled_vwap",
        "exec_type",
        "commission",
        "cost",
        "vwap",
        "vwap_diluted",
        "vwap_open",
        "market_value",
        "available",
        "available_now",
        "available_today",
        "order_frozen_today",
        "target_volume",
        "target_value",
        "target_percent",
        "order_frozen",
        "price",
        "volume",
    ]
    kinds = ("quote", "order", "trade", "command", "position", "account_observation", "order_set", "connection")
    return ChannelReplayCapability(
        supported=True,
        streams={
            kind: {
                "availability": "partial",
                "reason": "waiting_observation",
                "delivery": "query" if kind == "position" else "callback_and_query",
                "intermediate_states": "observed_only",
            }
            for kind in ("depth", "own_order", "own_trade", "position")
        }
        | {"market_trade": {"availability": "unavailable", "reason": "source_not_provided"}},
        attributes={kind: attributes for kind in kinds},
        native_identity={kind: ["cl_ord_id", "order_id", "ex_ord_id", "exec_id"] for kind in kinds},
        display={
            "order": [
                ReplayDisplayField(key="status", label="原生委托状态", kind="number"),
                ReplayDisplayField(key="order_business", label="委托业务类型", kind="number"),
                ReplayDisplayField(key="ord_rej_reason_detail", label="拒绝说明"),
                ReplayDisplayField(key="filled_amount", label="已成金额", kind="number", unit="CNY"),
                ReplayDisplayField(key="filled_vwap", label="原生成交均价", kind="number"),
            ],
            "position": [
                ReplayDisplayField(key="available_now", label="当前可卖", kind="number", unit="股"),
                ReplayDisplayField(key="available_today", label="当日可用", kind="number", unit="股"),
                ReplayDisplayField(key="order_frozen_today", label="当日委托冻结", kind="number", unit="股"),
                ReplayDisplayField(key="vwap", label="原生持仓均价", kind="number"),
                ReplayDisplayField(key="vwap_diluted", label="原生摊薄成本", kind="number"),
            ],
            "command": [ReplayDisplayField(key="target_volume", label="目标股数", kind="number", unit="股")],
        },
    )


def _tq_replay_capability() -> ChannelReplayCapability:
    """声明天勤快照差分，不将 SDK 合并后的观察当作完整逐笔流。"""
    identity = ["order_id", "exchange_order_id", "trade_id", "exchange_trade_id", "exchange_id", "instrument_id"]
    return ChannelReplayCapability(
        supported=True,
        streams={
            kind: {
                "availability": "partial",
                "reason": "waiting_observation",
                "delivery": "snapshot_diff" if kind != "position" else "query",
                "intermediate_states": "observed_only",
            }
            for kind in ("depth", "own_order", "own_trade", "position")
        }
        | {"market_trade": {"availability": "unavailable", "reason": "source_not_provided"}},
        attributes={
            "order": [
                "native_status",
                "last_msg",
                "price_type",
                "time_condition",
                "volume_condition",
                "is_online",
                "is_dead",
                "is_error",
                "account_mode",
            ],
            **{kind: ["account_mode"] for kind in ("quote", "command", "trade", "position")},
        },
        native_identity={
            kind: identity for kind in ("quote", "order", "trade", "command", "position", "order_set", "connection")
        },
        display={
            "order": [
                ReplayDisplayField(key="native_status", label="原生委托状态"),
                ReplayDisplayField(key="last_msg", label="原生委托说明"),
                ReplayDisplayField(key="price_type", label="价格条件"),
                ReplayDisplayField(key="time_condition", label="时间条件"),
                ReplayDisplayField(key="volume_condition", label="数量条件"),
                ReplayDisplayField(
                    key="account_mode",
                    label="账户模式",
                    kind="enum",
                    values={"live": "实盘", "kq": "快期模拟", "sim": "本地模拟"},
                ),
            ],
        },
    )


_LEVERAGE = ChannelLeverage(min=0, max=125, step=0.1)
_SINGLE_MAKER = AlgorithmReference(method="SINGLE-MAKER", params={})
_CN_STOCK_SESSION_WINDOWS = tuple(ChannelSessionWindow(start=start, end=end) for start, end in CN_STOCK_WINDOWS)
_CN_FUTURES_SESSION_WINDOWS = tuple(ChannelSessionWindow(start=start, end=end) for start, end in CN_FUTURES_WINDOWS)
_CN_FUTURES_NIGHT_WINDOWS = overnight_windows(CN_FUTURES_WINDOWS)
_CHINA_FUTURES_NIGHT = ChannelNightSchedule(
    label="夜盘",
    range_label="21:00–次日 02:30",
    close=(close_lead_hhmm(*_CN_FUTURES_NIGHT_WINDOWS[0]),),
    m15=tuple(rhythm_hhmm(_CN_FUTURES_NIGHT_WINDOWS, 15)),
    m60=tuple(rhythm_hhmm(_CN_FUTURES_NIGHT_WINDOWS, 60)),
)


def _contribution_target(config: dict[str, float], frame: pd.DataFrame) -> pd.DataFrame:
    """将策略权重与组合配置相乘并写入贡献度列."""
    strategies = cast("pd.Series", frame["strategy"])
    frame["contribution"] = frame["weight"] * strategies.map(config)
    return frame


def _gm_target(config: dict[str, float], frame: pd.DataFrame) -> pd.DataFrame:
    """计算掘金目标贡献度并保持 Axile 通用证券代码."""
    return _contribution_target(config, frame.copy())


def _tq_target(config: dict[str, float], frame: pd.DataFrame) -> pd.DataFrame:
    """计算天勤目标贡献度并保持 Axile 通用合约代码."""
    return _contribution_target(config, frame.copy())


_CTP_STEPS = tuple(
    ConnectionStepDefinition(id=key, label=label)
    for key, label in (
        ("trader_connection", "交易前置连接"),
        ("authenticate", "授权码认证"),
        ("trader_login", "交易账户登录"),
        ("settlement", "结算确认"),
        ("market_login", "行情登录"),
        ("query_orders", "订单查询"),
        ("query_trades", "成交查询"),
        ("query_positions", "持仓查询"),
        ("query_account", "资金查询"),
        ("channel_ready", "渠道会话就绪"),
    )
)
_GM_STEPS = tuple(
    ConnectionStepDefinition(id=key, label=label)
    for key, label in (
        ("environment", "准备掘金运行环境"),
        ("runtime", "启动策略桥"),
        ("account_login", "等待交易账户就绪"),
    )
)
_TQ_STEPS = tuple(
    ConnectionStepDefinition(id=key, label=label)
    for key, label in (
        ("api_connection", "建立天勤账户连接"),
        ("event_pump", "启动实时更新"),
    )
)


def _validate_gm_connection(config: BaseAccountConfig, report: ConnectionProgress) -> None:
    """在临时会话中复用 GM 工厂及真实账户核对。"""
    from axile.executor.connection_validation import validate_channel_connection

    validate_channel_connection("gm", config, report)


def _validate_tq_connection(config: BaseAccountConfig, report: ConnectionProgress) -> None:
    """在临时会话中复用 TQ 工厂及真实账户核对。"""
    from axile.executor.connection_validation import validate_channel_connection

    validate_channel_connection("tq", config, report)


def _create_ctp_executor(config: BaseAccountConfig):
    """根据已验证的 CTP 配置创建执行器。"""
    from axile.executor.ctp.ctp_execute import CTPExecutor

    if not isinstance(config, CTPAccountConfig):
        raise TypeError("CTP 渠道需要 CTPAccountConfig")
    executor = CTPExecutor(TradeChannel.CTP)
    executor.account_config = config
    return executor


def _validate_ctp_connection(config: BaseAccountConfig, report: ConnectionProgress) -> None:
    """延迟加载 SDK，并通过正常执行器验证一个临时会话。"""
    from axile.executor.ctp.connection_validation import validate_connection

    validate_connection(config, report)


def _create_gm_executor(config: BaseAccountConfig):
    """根据已验证的掘金配置创建执行器."""
    from axile.executor.gm import GMExecutor

    if not isinstance(config, GMAccountConfig):
        raise TypeError("GM 渠道需要 GMAccountConfig")
    return GMExecutor(config, connect=False)


def _create_tq_executor(config: BaseAccountConfig):
    """根据已验证的天勤配置创建执行器."""
    from axile.executor.tq import TQExecutor

    if not isinstance(config, TQAccountConfig):
        raise TypeError("天勤渠道需要 TQAccountConfig")
    return TQExecutor(config)


def _ctp_plugin() -> ChannelPlugin:
    """构造 CTP 内置渠道插件."""
    return ChannelPlugin(
        algorithm_scope_key=futures_algorithm_scope,
        descriptor=ChannelDescriptor(
            channel="ctp",
            cash_flow_sync=True,
            algorithm_scope=ChannelAlgorithmScope(
                pattern=FUTURES_SCOPE_PATTERN,
                example="ag",
                description="期货品种代码，如 ag、rb、TA；匹配该品种所有月份期货，不含期权和组合。",
            ),
            futures_close=close_capability(automatic=True),
            label="CTP",
            description="通过期货公司柜台连接国内期货市场",
            icon="chart-candlestick",
            market="ctp",
            schedule=ChannelSchedule(
                kind="cn_futures",
                night=_CHINA_FUTURES_NIGHT,
                windows=_CN_FUTURES_SESSION_WINDOWS,
            ),
            currency="CNY",
            units=ChannelUnits(
                quantity_kind="contract",
                quantity_label="手",
                quantity_max_decimals=0,
            ),
            ui=ChannelUi(
                position_value_label="货值",
                account_connect_lead="填写凭据后可验证账户连接；验证通过仅代表本次接入成功。",
            ),
            replay=_ctp_replay_capability(),
            connection_steps=_CTP_STEPS,
            connection_validation=ChannelConnectionValidation(
                notice="使用当前凭据连接柜台，查询账户并确认当日结算；不创建账户、不下单。",
                steps=tuple(
                    ConnectionStepDefinition(id=key, label=label)
                    for key, label in (
                        ("trader_connection", "交易前置连接"),
                        ("authenticate", "授权码认证"),
                        ("trader_login", "交易账户登录"),
                        ("settlement", "结算确认"),
                        ("instruments", "合约查询（螺纹钢）"),
                        ("market_login", "行情登录"),
                        ("query_orders", "订单查询"),
                        ("query_trades", "成交查询"),
                        ("query_positions", "持仓查询"),
                        ("query_account", "资金查询"),
                        ("channel_ready", "渠道会话就绪"),
                    )
                ),
            ),
            defaults=ChannelDefaults(
                account_control_preset="ctp",
                long_leverage=1,
                short_leverage=1,
                execution_timeout=180,
                trade_algorithm=AlgorithmReference(method="TARGET-POS-TASK", params={}),
                empty_positions_algorithm=AlgorithmReference(method="TARGET-POS-TASK", params={}),
            ),
            leverage=_LEVERAGE,
            portfolio=ChannelPortfolioPreset(market_label="期货", example_symbols=("rb2610", "ag2612")),
            account_form=ChannelAccountForm(
                fields=(
                    ChannelAccountField(
                        name="broker_id", label="期货公司代码", kind="identifier", width="half", placeholder="如 9999"
                    ),
                    ChannelAccountField(name="investor_id", label="投资者号", kind="identifier", width="half"),
                    ChannelAccountField(name="password", label="密码", kind="secret", width="full"),
                    ChannelAccountField(
                        name="td_front",
                        label="交易前置",
                        kind="endpoint",
                        width="full",
                        placeholder="tcp://...",
                        constraints=ChannelAccountFieldConstraints(
                            endpoint=ChannelEndpointConstraints(
                                scheme="required", allowed_schemes=("tcp",), port="required"
                            )
                        ),
                        clipboard=ChannelAccountFieldClipboard(role="trading", group="ctp-fronts"),
                    ),
                    ChannelAccountField(
                        name="md_front",
                        label="行情前置",
                        kind="endpoint",
                        width="full",
                        placeholder="tcp://...",
                        constraints=ChannelAccountFieldConstraints(
                            endpoint=ChannelEndpointConstraints(
                                scheme="required", allowed_schemes=("tcp",), port="required"
                            )
                        ),
                        clipboard=ChannelAccountFieldClipboard(role="market-data", group="ctp-fronts"),
                    ),
                    ChannelAccountField(name="app_id", label="应用 ID", kind="identifier", width="half"),
                    ChannelAccountField(name="auth_code", label="授权码", kind="secret", width="half"),
                )
            ),
            calendar=ChannelCalendar(calendar_id="china", label="中国交易日历"),
        ),
        account_config_model=CTPAccountConfig,
        create_executor=_create_ctp_executor,
        validate_connection=_validate_ctp_connection,
        target_transform=_contribution_target,
        execution_backend="process",
        required_modules=("openctp_ctp",),
        install_extra="ctp",
        max_parallel_symbols=None,
        order_param_model=OrderParamModel.OFFSET,
        canonicalize_symbol=canonicalize_cn_futures_symbol,
        quantize_target_quantity=quantize_cn_futures_quantity,
        requires_pre_connect_guard=True,
    )


def _connect_gm_account(config: BaseAccountConfig) -> None:
    """仅在显式连接账户时加载可选的 GM 模块。"""
    from axile.executor.gm.account_connection import connect_account

    connect_account(config)


def _gm_plugin() -> ChannelPlugin:
    """构造掘金内置渠道插件."""
    return ChannelPlugin(
        algorithm_scope_key=gm_algorithm_scope,
        connect_account=_connect_gm_account,
        schedule_settings_model=GMSessionSettings,
        legacy_schedule_field="gm_sessions",
        validate_connection=_validate_gm_connection,
        descriptor=ChannelDescriptor(
            connection_steps=_GM_STEPS,
            connection_validation=ChannelConnectionValidation(
                steps=_GM_STEPS, notice="使用当前配置建立临时会话并核对账户；不保存配置、不下单。"
            ),
            channel="gm",
            algorithm_scope=ChannelAlgorithmScope(
                pattern=GM_SCOPE_PATTERN, example="600000.SH", description="完整证券代码，如 600000.SH、SHSE.600000。"
            ),
            replay=_gm_replay_capability(),
            label="掘金",
            description="通过本机终端或 RPC 服务连接掘金量化",
            icon="landmark",
            market="ashare",
            schedule=ChannelSchedule(kind="cn_stock", windows=_CN_STOCK_SESSION_WINDOWS),
            currency="CNY",
            units=ChannelUnits(
                quantity_kind="share",
                quantity_label="股",
                quantity_max_decimals=0,
            ),
            ui=ChannelUi(
                account_connect_lead="账号与 Token 两种连接方式共用；本机终端与 RPC 地址只选一种。",
                leverage_note="A 股仅多头，是投入权益比例",
                long_leverage_label="仓位系数（做多）",
                show_short_leverage=False,
                position_value_label="市值",
            ),
            defaults=ChannelDefaults(
                account_control_preset="default",
                long_leverage=1,
                short_leverage=1,
                execution_timeout=180,
                trade_algorithm=_SINGLE_MAKER,
                empty_positions_algorithm=_SINGLE_MAKER,
            ),
            leverage=_LEVERAGE,
            portfolio=ChannelPortfolioPreset(
                market_label="A股",
                example_symbols=("600000.SH", "000001.SZ"),
            ),
            account_form=ChannelAccountForm(
                fields=(
                    ChannelAccountField(name="account_id", label="账号 ID", kind="identifier", width="half"),
                    ChannelAccountField(name="token", label="Token", kind="secret", width="half"),
                    ChannelAccountField(
                        name="connection_mode",
                        label="连接方式",
                        kind="select",
                        width="full",
                        default="terminal",
                        presentation="conditional_reveal",
                        help="只需配置其中一种。",
                        options=(
                            ChannelAccountOption(
                                value="terminal",
                                label="本机终端",
                                description="Axile 与掘金终端同机，填写安装目录并由 Axile 检查或启动。",
                            ),
                            ChannelAccountOption(
                                value="service",
                                label="终端 RPC 地址",
                                description="Axile 连接已经运行的终端，支持同机或异机部署。",
                            ),
                        ),
                    ),
                    ChannelAccountField(
                        name="terminal_path",
                        label="本机终端目录",
                        kind="directory",
                        width="full",
                        placeholder=r"C:\Program Files\GoldMiner3",
                        help="填写包含 goldminer3.exe 的安装目录。",
                        visible_when=ChannelAccountFieldCondition(field="connection_mode", equals="terminal"),
                    ),
                    ChannelAccountField(
                        name="serv_addr",
                        label="终端 RPC 地址",
                        kind="endpoint",
                        width="full",
                        placeholder="127.0.0.1:7001",
                        help=(
                            r"先启动掘金终端。地址取自安装目录下 resources\app\gmserv.json 的 "
                            "default.hostAddr 和 default.rpcPort；异机连接需填写 Axile 可访问的 IP，"
                            "不能使用 127.0.0.1。"
                        ),
                        visible_when=ChannelAccountFieldCondition(field="connection_mode", equals="service"),
                        constraints=ChannelAccountFieldConstraints(
                            endpoint=ChannelEndpointConstraints(scheme="forbidden", port="required")
                        ),
                        clipboard=ChannelAccountFieldClipboard(role="rpc"),
                    ),
                ),
            ),
            calendar=ChannelCalendar(calendar_id="china", label="中国交易日历"),
        ),
        account_config_model=GMAccountConfig,
        create_executor=_create_gm_executor,
        target_transform=_gm_target,
        execution_backend="process",
        required_modules=("gm",),
        install_extra="gm",
        requires_pre_connect_guard=True,
        max_parallel_symbols=None,
        order_param_model=OrderParamModel.DIRECTIONAL,
    )


def _tq_plugin() -> ChannelPlugin:
    """构造天勤 TqSdk 内置渠道插件."""
    live = ChannelAccountFieldCondition(field="account_mode", equals="live")
    sim = ChannelAccountFieldCondition(field="account_mode", equals="sim")
    return ChannelPlugin(
        algorithm_scope_key=futures_algorithm_scope,
        validate_connection=_validate_tq_connection,
        descriptor=ChannelDescriptor(
            connection_steps=_TQ_STEPS,
            connection_validation=ChannelConnectionValidation(
                steps=_TQ_STEPS, notice="使用当前配置建立临时会话并核对账户；不保存配置、不下单。"
            ),
            channel="tq",
            algorithm_scope=ChannelAlgorithmScope(
                pattern=FUTURES_SCOPE_PATTERN,
                example="SHFE.ag",
                description="期货品种代码，如 SHFE.ag、ag、CZCE.TA；匹配所有月份期货，不含期权和组合。",
            ),
            replay=_tq_replay_capability(),
            futures_close=close_capability(automatic=False),
            label="天勤",
            description="通过天勤连接国内期货、期权与组合市场",
            icon="radio-tower",
            market="ctp",
            schedule=ChannelSchedule(
                kind="cn_futures",
                night=_CHINA_FUTURES_NIGHT,
                windows=_CN_FUTURES_SESSION_WINDOWS,
            ),
            currency="CNY",
            units=ChannelUnits(
                quantity_kind="contract",
                quantity_label="手",
                quantity_max_decimals=0,
            ),
            ui=ChannelUi(
                account_connect_lead="选择账户模式后填写对应凭据。",
                position_value_label="货值",
            ),
            defaults=ChannelDefaults(
                account_control_preset="default",
                long_leverage=1,
                short_leverage=1,
                execution_timeout=180,
                trade_algorithm=AlgorithmReference(method="TARGET-POS-TASK", params={}),
                empty_positions_algorithm=AlgorithmReference(method="TARGET-POS-TASK", params={}),
            ),
            leverage=_LEVERAGE,
            portfolio=ChannelPortfolioPreset(market_label="期货", example_symbols=("rb2610", "ag2612")),
            account_form=ChannelAccountForm(
                fields=(
                    ChannelAccountField(
                        name="account_mode",
                        label="账户模式",
                        kind="select",
                        width="full",
                        presentation="conditional_reveal",
                        help="选择账户运行方式；天勤账号与密码由三种模式共用。",
                        options=(
                            ChannelAccountOption(
                                value="live",
                                label="实盘账户",
                                description="连接期货公司实盘账户，需填写交易账户凭据。",
                            ),
                            ChannelAccountOption(
                                value="kq",
                                label="快期模拟",
                                description="使用快期模拟账户，无需额外交易账户凭据。",
                            ),
                            ChannelAccountOption(
                                value="sim",
                                label="本地模拟",
                                description="由 Axile 在当前 Worker 中维护模拟资金与持仓。",
                            ),
                        ),
                    ),
                    ChannelAccountField(name="tq_username", label="天勤账号", kind="identifier", width="half"),
                    ChannelAccountField(name="tq_password", label="天勤密码", kind="secret", width="half"),
                    ChannelAccountField(
                        name="broker_name", label="期货公司名称", kind="text", width="half", visible_when=live
                    ),
                    ChannelAccountField(
                        name="account_id", label="交易账户", kind="identifier", width="half", visible_when=live
                    ),
                    ChannelAccountField(
                        name="account_password",
                        label="交易密码",
                        kind="secret",
                        width="full",
                        visible_when=live,
                    ),
                    ChannelAccountField(
                        name="initial_balance",
                        label="初始资金",
                        kind="money",
                        width="full",
                        default=10_000_000,
                        visible_when=sim,
                        constraints=ChannelAccountFieldConstraints(number=ChannelNumberConstraints(gt=0)),
                    ),
                ),
                notices=(
                    ChannelAccountNotice(
                        tone="warning",
                        text="本地模拟状态保存在当前 Worker 中，日盘或夜盘重建后资金与持仓会重置。",
                    ),
                ),
            ),
            calendar=ChannelCalendar(calendar_id="china", label="中国交易日历"),
        ),
        account_config_model=TQAccountConfig,
        create_executor=_create_tq_executor,
        target_transform=_tq_target,
        execution_backend="process",
        required_modules=("tqsdk",),
        install_extra="tqsdk",
        max_parallel_symbols=None,
        order_param_model=OrderParamModel.OFFSET,
        canonicalize_symbol=canonicalize_cn_futures_symbol,
        quantize_target_quantity=quantize_cn_futures_quantity,
    )


def builtin_channel_plugins() -> tuple[ChannelPlugin, ...]:
    """
    按固定展示顺序返回公开包内置渠道.

    Returns
    -------
    tuple[ChannelPlugin, ...]
        依次为 CTP、掘金与天勤渠道插件。
    """
    return (_ctp_plugin(), _gm_plugin(), _tq_plugin())
