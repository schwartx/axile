"""CTP 字段格式校验；只拒绝非法输入，不改写交易标识。"""

import re


class CTPInputValidationError(ValueError):
    """可展示的发送前校验错误。"""

    @property
    def execution_error(self) -> str:
        """返回不含凭据原值的拦截提示。"""
        return f"CTP 参数无效，已在发送前拦截：{self}"


def validate_credential(field: str, value: object) -> str:
    """按字段校验凭据；AppID 连字符沿用允许规则，待报告歧义确认。"""
    limits = {"broker_id": 10, "investor_id": 12, "app_id": 32, "auth_code": 16}
    pattern = r"[A-Za-z0-9_.-]+" if field == "app_id" else r"[A-Za-z0-9]+"
    if not isinstance(value, str) or len(value) > limits[field] or re.fullmatch(pattern, value) is None:
        raise CTPInputValidationError(f"{field}格式不正确或超过允许长度")
    return value


def validate_config(config: object) -> None:
    """防止绕过模型校验的配置进入原生请求。"""
    for field in ("broker_id", "investor_id", "app_id", "auth_code"):
        validate_credential(field, getattr(config, field, None))


def validate_instrument(value: object, *, allow_all: bool = False) -> str:
    """接受单合约和套利组合；空串仅用于显式全量查询。"""
    if value == "" and allow_all:
        return ""
    leg = r"[A-Za-z][A-Za-z0-9]*(?:-[CPcp]-[0-9]+(?:\.[0-9]+)?)?"
    pattern = rf"(?:{leg}|(?:SP|SPC|SPD|IPS) {leg}&{leg})"
    if not isinstance(value, str) or len(value) > 80 or re.fullmatch(pattern, value) is None:
        raise CTPInputValidationError("合约代码格式不正确或超过允许长度")
    return value


def validate_cancel_id(value: object) -> str:
    """校验本渠道生成的稳定订单编号；不更改合法冒号分隔符。"""
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9]{8}:[0-9]+:[0-9]+:[0-9]{1,12}", value) is None
        or len(value) > 200
    ):
        raise CTPInputValidationError("订单编号格式不正确")
    return value
