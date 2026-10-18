"""
统一输入模型使用的账户配置定义.

按交易渠道拆分账户配置模型，避免 ``UnifiedStandardInput`` 主文件同时承载
账户字段定义与输入构造规则。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Literal, override
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from axile.common.trade_channel import TradeChannel
from axile.executor.ctp_input_validation import validate_credential

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_DOMAIN_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_INVALID_PATH = re.compile(r'[<>:"|?*]')


def _validate_single_line(value: str, label: str) -> str:
    """校验必填连接值为非空单行文本。"""
    if not value.strip():
        raise ValueError(f"{label}不能为空")
    if _CONTROL_CHARACTERS.search(value):
        raise ValueError(f"{label}只能填写一项内容")
    return value


def _valid_endpoint_host(host: str) -> bool:
    """判断地址主机部分是否为合法 IP 或域名。"""
    if not host or any(character.isspace() for character in host) or _CONTROL_CHARACTERS.search(host):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if re.fullmatch(r"[\d.]+", host) or len(host) > 253:
        return False
    return all(_DOMAIN_LABEL.fullmatch(label) for label in host.split("."))


def validate_connection_endpoint(
    value: str,
    *,
    scheme: Literal["required", "optional", "forbidden"],
    allowed_schemes: frozenset[str],
    port: Literal["required", "optional"],
    allow_path: bool = False,
) -> str:
    """校验连接地址结构，不执行任何网络访问。"""
    value = _validate_single_line(value, "地址").strip()
    scheme_match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", value)
    has_scheme = scheme_match is not None
    if scheme == "required" and not has_scheme:
        raise ValueError("地址必须包含协议")

    try:
        parsed = urlsplit(value if has_scheme else f"//{value}")
        parsed_port = parsed.port
    except ValueError as exc:
        if "Port out of range" in str(exc) or "port" in str(exc).lower():
            raise ValueError("端口必须是 1–65535 的整数") from None
        raise ValueError("主机地址格式不正确") from None

    if not parsed.hostname:
        raise ValueError("地址必须包含主机")
    raw_authority = parsed.netloc.rsplit("@", 1)[-1]
    has_port_marker = (
        raw_authority.startswith("[")
        and "]:" in raw_authority
        or (not raw_authority.startswith("[") and ":" in raw_authority)
    )
    if has_port_marker and parsed_port is None:
        raise ValueError("端口必须是 1–65535 的整数")
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise ValueError("端口必须是 1–65535 的整数")
    if (port == "required" or not has_scheme) and parsed_port is None:
        raise ValueError("地址必须包含端口")

    parsed_scheme = parsed.scheme.lower()
    if scheme == "forbidden" and has_scheme:
        raise ValueError("地址不能包含协议")
    if has_scheme and parsed_scheme not in allowed_schemes:
        raise ValueError(f"不支持 {parsed_scheme}:// 协议")
    if not _valid_endpoint_host(parsed.hostname):
        raise ValueError("主机地址格式不正确")
    if not allow_path and (parsed.path or parsed.query or parsed.fragment):
        raise ValueError("地址不能包含路径、查询参数或片段")
    return value


def validate_windows_directory(value: str) -> str:
    """校验并整理 GM 支持的 Windows 绝对目录。"""
    value = _validate_single_line(value, "终端目录").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    if _WINDOWS_DRIVE_PATH.match(value):
        rest = value[3:]
        if not _WINDOWS_INVALID_PATH.search(rest):
            return value
    unc = re.match(r"^\\\\([^\\/]+)[\\/]([^\\/]+)(.*)$", value)
    if unc and not _WINDOWS_INVALID_PATH.search("".join(unc.groups())):
        return value
    raise ValueError("请输入 Windows 绝对路径")


def _channel_value(channel: object) -> str:
    """
    返回用于日志与序列化的规范化渠道字符串.

    Parameters
    ----------
    channel : object
        渠道对象或原始渠道值。

    Returns
    -------
    str
        规范化后的渠道字符串。
    """
    channel_value = getattr(channel, "value", None)
    if isinstance(channel_value, str):
        return channel_value
    return str(channel)


class BaseAccountConfig(BaseModel):
    """
    基础账户配置抽象模型.

    Attributes
    ----------
    channel_type : TradeChannel
        账户所属的交易渠道类型。
    """

    model_config = ConfigDict(extra="forbid")

    channel_type: TradeChannel = Field(..., description="交易渠道类型")

    @override
    def __str__(self) -> str:
        """返回便于记录日志的简要字符串表示."""
        return f"{_channel_value(self.channel_type)}AccountConfig"


class CTPAccountConfig(BaseAccountConfig):
    """
    CTP 期货账户配置.

    Attributes
    ----------
    broker_id : str
        经纪公司代码。
    investor_id : str
        投资者代码。
    password : str
        登录密码。
    """

    quote_max_age_seconds: float = Field(
        default=5.0, gt=0, allow_inf_nan=False, description="行情最大年龄（秒），同时校验交易所时间和本地接收时间"
    )

    broker_id: str = Field(..., description="经纪公司代码")
    investor_id: str = Field(..., description="投资者代码")
    password: str = Field(..., description="密码")

    td_front: str = Field(..., description="交易服务器地址")
    md_front: str = Field(..., description="行情服务器地址")
    product_info: str | None = Field(None, description="产品信息")
    auth_code: str = Field(..., description="认证码")
    app_id: str = Field(..., description="应用ID")

    @field_validator("broker_id", "investor_id", "password", "auth_code", "app_id")
    @classmethod
    def validate_required_field(cls, value: str, info: object) -> str:
        """校验 CTP 凭据字段非空且为单行。"""
        field_name = getattr(info, "field_name", "CTP 字段")
        value = _validate_single_line(value, str(field_name))
        if field_name != "password":
            return validate_credential(str(field_name), value)
        return value

    @field_validator("td_front", "md_front")
    @classmethod
    def validate_front(cls, value: str) -> str:
        """校验 CTP 前置地址为带端口的 TCP 地址。"""
        return validate_connection_endpoint(
            value,
            scheme="required",
            allowed_schemes=frozenset({"tcp"}),
            port="required",
        )

    @model_validator(mode="before")
    @classmethod
    def set_channel_type(cls, data: dict[str, object] | BaseAccountConfig) -> dict[str, object] | BaseAccountConfig:
        """
        在预校验阶段补齐渠道类型.

        Parameters
        ----------
        data : dict[str, object] | BaseAccountConfig
            原始账户配置数据或已构造的账户对象。

        Returns
        -------
        dict[str, object] | BaseAccountConfig
            补齐 ``channel_type`` 后的数据对象。
        """
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized["channel_type"] = TradeChannel.CTP
        return normalized


class GMAccountConfig(BaseAccountConfig):
    """
    掘金账户配置.

    Attributes
    ----------
    account_id : str
        掘金账户标识。
    token : str
        掘金终端令牌。
    terminal_path : str | None
        本地终端路径。
    serv_addr : str | None
        远程服务地址。
    """

    connection_mode: Literal["terminal", "service"]
    account_id: str = Field(..., description="掘金账户ID")
    token: str = Field(..., description="掘金终端令牌")
    terminal_path: str | None = Field(None, description="掘金终端目录")
    serv_addr: str | None = Field(None, description="掘金服务地址")

    @field_validator("account_id", "token")
    @classmethod
    def validate_required_field(cls, value: str, info: object) -> str:
        """校验 GM 账号与 Token 非空且为单行。"""
        field_name = getattr(info, "field_name", "GM 字段")
        return _validate_single_line(value, str(field_name))

    @field_validator("terminal_path")
    @classmethod
    def validate_terminal_path(cls, value: str | None) -> str | None:
        """校验本机终端路径为 Windows 绝对目录。"""
        return None if value is None else validate_windows_directory(value)

    @field_validator("serv_addr")
    @classmethod
    def validate_service_address(cls, value: str | None) -> str | None:
        """校验远程终端地址为裸主机端口。"""
        if value is None:
            return None
        return validate_connection_endpoint(
            value,
            scheme="forbidden",
            allowed_schemes=frozenset(),
            port="required",
        )

    @model_validator(mode="before")
    @classmethod
    def set_channel_type(cls, data: dict[str, object] | BaseAccountConfig) -> dict[str, object] | BaseAccountConfig:
        """
        在预校验阶段补齐渠道类型.

        Parameters
        ----------
        data : dict[str, object] | BaseAccountConfig
            原始账户配置数据或已构造的账户对象。

        Returns
        -------
        dict[str, object] | BaseAccountConfig
            补齐 ``channel_type`` 后的数据对象。
        """
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized["channel_type"] = TradeChannel.GM
        mode = normalized.get("connection_mode")
        if mode == "terminal":
            normalized.pop("serv_addr", None)
        elif mode == "service":
            normalized.pop("terminal_path", None)
        return normalized

    @model_validator(mode="after")
    def validate_connection_target(self) -> "GMAccountConfig":
        """
        校验连接目标配置是否合法.

        Returns
        -------
        GMAccountConfig
            校验通过后的当前账户配置对象。

        Raises
        ------
        ValueError
            当 ``terminal_path`` 与 ``serv_addr`` 同时为空或同时存在时抛出。
        """
        target = self.terminal_path if self.connection_mode == "terminal" else self.serv_addr
        if target is None:
            raise ValueError(f"{self.connection_mode} 模式缺少连接目标")
        return self


class TQAccountConfig(BaseAccountConfig):
    """天勤 TqSdk 账户配置."""

    account_mode: Literal["live", "kq", "sim"]
    tq_username: str = Field(min_length=1, description="天勤账号")
    tq_password: str = Field(min_length=1, description="天勤密码")
    broker_name: str | None = Field(None, description="期货公司名称")
    account_id: str | None = Field(None, description="交易账户")
    account_password: str | None = Field(None, description="交易密码")
    initial_balance: float = Field(10_000_000.0, gt=0, description="本地模拟初始资金")

    @field_validator("tq_username", "tq_password")
    @classmethod
    def validate_tq_credential(cls, value: str, info: object) -> str:
        """校验天勤公共凭据非空且为单行。"""
        field_name = getattr(info, "field_name", "天勤凭据")
        return _validate_single_line(value, str(field_name))

    @field_validator("broker_name", "account_id", "account_password")
    @classmethod
    def validate_live_credential(cls, value: str | None, info: object) -> str | None:
        """校验已填写的实盘凭据非空且为单行。"""
        if value is None:
            return None
        field_name = getattr(info, "field_name", "实盘凭据")
        return _validate_single_line(value, str(field_name))

    @model_validator(mode="before")
    @classmethod
    def set_channel_type(cls, data: dict[str, object] | BaseAccountConfig) -> dict[str, object] | BaseAccountConfig:
        """补齐 TqSdk 渠道类型并移除非当前模式字段."""
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        normalized["channel_type"] = TradeChannel.TQ
        mode = normalized.get("account_mode")
        if mode != "live":
            for key in ("broker_name", "account_id", "account_password"):
                normalized.pop(key, None)
        if mode != "sim":
            normalized.pop("initial_balance", None)
        return normalized

    @model_validator(mode="after")
    def validate_mode_fields(self) -> "TQAccountConfig":
        """校验实盘模式的交易账户凭据."""
        if self.account_mode == "live":
            missing = [name for name in ("broker_name", "account_id", "account_password") if not getattr(self, name)]
            if missing:
                raise ValueError(f"TqAccount 模式缺少必填字段: {', '.join(missing)}")
        return self


AccountConfig = BaseAccountConfig
