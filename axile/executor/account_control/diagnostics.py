"""保存风控评估快照，并集中生成面向用户的中文说明。"""

from dataclasses import asdict, dataclass

OPERATION_LABELS = {
    "place_order": "下单",
    "cancel_order": "撤单",
    "cancel_order_ctp": "撤单",
    "query_order": "查询订单",
    "query_orders": "查询订单",
    "query_trades": "查询成交",
    "ctp_query_trades": "查询成交",
    "query_account": "查询账户",
    "query_positions": "查询持仓",
    "query_depth_market_data": "查询行情快照",
    "query_commission_rates": "查询手续费率",
    "query_instruments": "查询合约",
    "authenticate": "认证",
    "trader_login": "交易登录",
    "query_settlement_status": "查询结算状态",
    "confirm_settlement": "确认结算",
    "query_settlement_info": "查询结算信息",
    "option_exercise": "期权行权",
    "option_abandon": "放弃期权行权",
    "option_self_close": "期权自对冲",
    "cancel_option_exercise": "撤销期权行权",
    "cancel_option_abandon": "撤销放弃期权行权",
    "cancel_option_self_close": "撤销期权自对冲",
}
GROUP_LABELS = {"ctp_td_global": "CTP 交易柜台共享操作组"}
RULE_LABELS = {"per_day": "每日次数上限", "per_minute": "每分钟次数上限", "min_interval_ms": "最小操作间隔"}


def operation_label(operation: str) -> str:
    """返回操作中文名，未登记操作保留可定位的原始标识。"""
    return OPERATION_LABELS.get(operation, f"自定义操作（{operation}）")


@dataclass(frozen=True)
class AccountControlHit:
    """保存触发瞬间的值；次数是风控放行计数，并非渠道受理或成交次数。"""

    rule_kind: str
    scope_type: str
    scope_key: str
    scope_symbol: str | None
    operation: str
    current_value: int
    limit: int
    unit: str
    action: str
    evaluated_at: str
    account_id: int | None
    symbol: str | None
    control_date: str | None = None
    timezone: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    retry_after_ms: int | None = None

    @property
    def identity(self) -> tuple[str, str, str | None, str]:
        """返回单个请求等待原因的去重键。"""
        return self.scope_type, self.scope_key, self.scope_symbol, self.rule_kind

    @property
    def rule_label(self) -> str:
        """返回包含范围的中文规则名。"""
        scope = {"account": "账户级", "symbol": "合约级"}.get(self.scope_type)
        if scope is None:
            scope = "账户共享操作组：" + GROUP_LABELS.get(self.scope_key, f"共享操作组（{self.scope_key}）")
        operation = "共享操作" if self.scope_type == "group" else operation_label(self.operation)
        rule = {
            "per_day": f"每日{operation}次数已达上限",
            "per_minute": f"每分钟{operation}次数已达上限",
            "min_interval_ms": f"最小{operation}间隔不足",
        }[self.rule_kind]
        return f"{scope}，{rule}"

    def message(self) -> str:
        """生成与此快照完全一致的单行中文提示。"""
        parts = ["账户风控拦截" if self.action == "block" else "账户风控等待额度"]
        if self.account_id is not None:
            parts.append(f"账户 {self.account_id}")
        if self.symbol is not None:
            parts.append(f"合约 {self.symbol}")
        parts.extend([operation_label(self.operation), self.rule_label])
        if self.rule_kind == "min_interval_ms":
            parts.append(
                f"已间隔 {self.current_value} 毫秒，要求至少 {self.limit} 毫秒，还需等待 {self.retry_after_ms} 毫秒"
            )
        else:
            parts.append(f"当前已用 {self.current_value} 次，上限 {self.limit} 次")
            if self.limit == 0:
                parts.append("配置上限为 0，当前规则禁止该操作")
            parts.append(
                f"统计日期 {self.control_date}，统计窗口 {self.window_start} 至 {self.window_end}（{self.timezone}）"
            )
            if self.limit != 0:
                parts.append(f"重置时间 {self.window_end}")
                if self.action == "wait":
                    parts.append(f"预计等待 {self.retry_after_ms} 毫秒后重新评估")
        parts.append("本次请求未提交" if self.action == "block" else "等待后重新评估额度")
        return "；".join(parts)

    def to_metadata(self) -> dict[str, object]:
        """导出可直接写入事件 JSON 的快照和中文说明。"""
        return {**asdict(self), "message": self.message()}
