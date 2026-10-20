"""导出边界的递归脱敏；分享材料采用结构化字段白名单。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from axile.server.evidence.models import EvidenceProfile

REDACTION_VERSION = 1
_SECRET_PARTS = (
    "password",
    "passwd",
    "apikey",
    "secret",
    "token",
    "authcode",
    "authorization",
    "credential",
    "privatekey",
    "accesskey",
    "accountconfig",
    "feishukey",
    "webhook",
    "notificationcode",
    "customcalcpycode",
    "pythoncode",
    "sourcecode",
    "traceback",
)
_IDENTITY_KEYS = {
    "accountname",
    "investorid",
    "userid",
    "brokerid",
    "loginid",
    "username",
    "email",
    "phone",
    "ipaddress",
    "macaddress",
    "clientipaddress",
    "traderfront",
    "mdfront",
    "endpoint",
    "hostname",
    "file",
    "path",
    "home",
    "remark",
}
_FREE_TEXT_KEYS = {
    "error",
    "errormsg",
    "errormessage",
    "message",
    "msg",
    "technicaldetail",
    "outcomereason",
    "description",
    "exception",
    "debug",
    "reason",
    "cancelreason",
}
_SAFE_KEYS = set(
    """
id execution_id account_id key execution_kind kind context_kind trigger_source status output_status outcome
business_facts_ref input_id asset_id view fact_path
created_at started_at finished_at cancel_requested_at cancel_reason terminate_mode is_success
event_uid event_type channel algorithm schema_version reason_family reason_code symbol intent_id
order_id client_order_id ts_local_created ts_exchange ts_persisted seq details decision result
raw_input raw_result input inputs content artifact_type metadata operation counted control_date payload
occurred_at_ms bucket_type bucket_start scope_type delta_count delta_uid instance_id timezone
started_at_ms finished_at_ms source source_before source_after account_assets account before after
available_cash total_asset market_value positions available_volume volume frozen_volume direction
update_time currency broker_timestamp quantity_kind quantity_label quantity_max_decimals price_label
notional_label method params algorithm_name algorithm_params trade_rules symbol_algorithms
curr_target last_target strategy_target account_target sizing_context long_leverage short_leverage
weight_precision forbidden_symbols risk_symbols market brokerage algorithm_method success
execution_time symbol_results target_volume final_volume first_tick target_sizing memory
orders trades summary reconciliation symbols before_quantity after_quantity target filled moved
drift reached attained_ratio avg_price price avg_fill_price order_type filled_volume remaining_volume
trade_id trade_time trade_volume trade_price trade_value extra front_id session_id order_ref
order_sysid order_sys_id exchange_id trading_day exchange_order_id order_local_id native_order_id
offset offset_flag hedge_flag time_condition volume_condition order_status submit_status
return_code error_id error_code error_stage request_id request_seq request_identity order_identity
native_identity native_trade_id native_trade_time cl_ord_id price_type liquidity commission
commission_asset fee fee_asset sizing_mode equity account_weight strategy_weight account_multiplier
reference_price unit_multiplier unit_notional target_notional raw_quantity target_quantity
current_quantity quantity_step min_quantity min_notional lot_size max_volume min_volume tick_size
min_market_order_volume max_market_order_volume min_limit_order_volume max_limit_order_volume
source_currency target_currency multiplier value_conversion notional_kind conversion_rate
exchange_rate native_currency native_value account_currency account_value valuation_source
reason value limit on_trigger per_minute per_day min_interval_ms account symbol_overrides
operations groups operation_groups priority policy fingerprint actual_policy actual_fingerprint
account_control_hit account_control_wait_hits scope rule waited_ms wait_ms estimated_wait_ms
cancel_failed_order_ids cancel_unconfirmed cancel_attempted forced trigger acked_at requested_at
execution_version environment python_version axile_version packages executor_module executor_class
algorithm implementation_sha256 captured_at version distribution entry_point name provider
n_orders n_trades ordered executed fill_ratio arrival_mid slippage_bps tca price_data
bid_price ask_price last_price timestamp lower_limit_price upper_limit_price instrument
source_path source_record_id source_kind observed_at target_calculated_at account_snapshot
account_snapshot_before trade_rules_snapshot target_snapshot standard_input execution_summary
symbol_decision_reason_code signed_volume source_type precision max_order_volume min_order_volume
level component context function line process thread create_time sizing value_currency
price_currency native_value_currencies limits trigger_source source_ref
futures_close_policy futures_close_decision mode fallback overrides product priority preset_version
yesterday_cost today_cost fallback_reason queried_at exchange observed_at legs
market_order_price_type limit_order_price_type offset_priority close_order_type open_order_type
max_wait_seconds order_timeout_seconds order_volume order_price strategy_name
""".split()
)
_DYNAMIC_MAPS = {
    "symbol_results",
    "curr_target",
    "last_target",
    "strategy_target",
    "account_target",
    "trade_rules",
    "symbol_algorithms",
    "symbol_overrides",
    "overrides",
    "packages",
    "operations",
    "groups",
    "operation_groups",
}
_SAFE_NORMALIZED = {re.sub(r"[^a-z0-9]", "", key.lower()) for key in _SAFE_KEYS}
_PAIR = re.compile(
    r"(?i)\b(password|passwd|api[_-]?key|secret[_-]?key|api[_-]?secret|access[_-]?token|token|"
    r"authorization|auth[_-]?code)\b(\s*['\"]?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)"
)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/_.=-]+")
_URL = re.compile(r"(?:https?|wss?|tcp)://[^\s<>\"']+")
_PATH = re.compile(r"(?<!\w)(?:/[A-Za-z0-9_.~-]+){2,}|[A-Za-z]:\\(?:[^\s\\]+\\)*[^\s\\]*")


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


@dataclass
class EvidenceRedactor:
    """不修改数据库原值，记录被移除字段的路径和规则。"""

    profile: EvidenceProfile
    account_id: int
    account_name: str
    changes: dict[str, str] = field(default_factory=dict)
    account_aliases: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        """将所选账户固定为 A，其他已出现的账户身份保持独立关联。"""
        self.account_aliases[str(self.account_id)] = "account-A"

    def _drop(self, path: str, reason: str) -> None:
        self.changes[path] = reason

    def _reason(self, key: str, *, dynamic: bool) -> str | None:
        normalized = _key(key)
        if dynamic:
            return None
        if normalized in {"pwd", "code", "script", "stack", "stacktrace"} or any(
            part in normalized for part in _SECRET_PARTS
        ):
            return "credential_or_code"
        if self.profile is EvidenceProfile.INTERNAL:
            return None
        if normalized in _IDENTITY_KEYS:
            return "identity_or_address"
        if normalized in _FREE_TEXT_KEYS:
            return "unstructured_text"
        if not dynamic and key not in _SAFE_KEYS and normalized not in _SAFE_NORMALIZED:
            return "unregistered_field"
        return None

    def _text(self, value: str, path: str) -> str:
        result = _PAIR.sub(r"\1\2[REDACTED]", value)
        result = _BEARER.sub("[REDACTED_AUTH]", result)
        result = _URL.sub("[REDACTED_URL]", result)
        result = _PATH.sub("[REDACTED_PATH]", result)
        if result != value:
            self._drop(path, "text_redacted")
        return result

    def clean(self, value: Any, path: str = "$", *, parent: str = "") -> Any:
        """递归处理嵌套字典、列表和文本，保留订单与成交关联字段。"""
        if isinstance(value, list):
            return [self.clean(item, f"{path}[{i}]", parent=parent) for i, item in enumerate(value)]
        if not isinstance(value, dict):
            return self._text(value, path) if isinstance(value, str) else value
        result = {}
        for key, item in value.items():
            key = str(key)
            child = f"{path}.{key}"
            dynamic = parent in _DYNAMIC_MAPS
            if dynamic and not self._valid_dynamic_value(parent, item):
                if self.profile is EvidenceProfile.SHAREABLE:
                    self._drop(child, "invalid_dynamic_value")
                    continue
                dynamic = False
            if (
                self.profile is EvidenceProfile.SHAREABLE
                and key == "name"
                and parent
                in {
                    "account",
                    "account_assets",
                    "input",
                    "raw_input",
                    "payload",
                }
            ):
                self._drop(child, "identity_or_address")
                continue
            reason = self._reason(key, dynamic=dynamic)
            if reason:
                self._drop(child, reason)
                continue
            if not dynamic and _key(key) == "accountid" and self.profile is EvidenceProfile.SHAREABLE:
                result[key] = (
                    None
                    if item is None
                    else self.account_aliases.setdefault(
                        str(item),
                        f"account-other-{len(self.account_aliases)}",
                    )
                )
                self._drop(child, "account_alias")
            else:
                result[key] = self.clean(item, child, parent=key)
        return result

    @staticmethod
    def _valid_dynamic_value(parent: str, value: Any) -> bool:
        """动态键是合约或分发包名，不能误把含 TOKEN 的合约当作密钥字段。"""
        if parent in {"curr_target", "last_target", "strategy_target", "account_target"}:
            return value is None or isinstance(value, (int, float))
        if parent == "packages":
            return value is None or isinstance(value, str)
        if parent == "operation_groups":
            return isinstance(value, list) and all(isinstance(item, str) for item in value)
        return isinstance(value, dict)

    def describe(self) -> dict[str, Any]:
        """脱敏清单只记录路径和规则，避免再次带出被移除的值。"""
        # 分享版的动态字段名可能本身包含敏感值；清单只公开规则计数。
        if self.profile is EvidenceProfile.SHAREABLE:
            counts: dict[str, int] = {}
            for reason in self.changes.values():
                counts[reason] = counts.get(reason, 0) + 1
            return {"version": REDACTION_VERSION, "profile": self.profile.value, "counts": counts}
        return {
            "version": REDACTION_VERSION,
            "profile": self.profile.value,
            "fields": [
                {"path": self._text(path, "$redaction"), "rule": reason} for path, reason in list(self.changes.items())
            ],
        }
