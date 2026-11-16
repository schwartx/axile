"""系统时间检查接口模型及共享校验。"""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Annotated, Literal

from msgspec import Meta

from axile.server.cron import parse_cron_expr

SOURCES = {
    "aliyun": "ntp.aliyun.com",
    "tencent": "ntp.tencent.com",
    "tuna": "ntp.tuna.tsinghua.edu.cn",
}


@dataclass(kw_only=True)
class TimeCheckSettings:
    """检查配置；不接受 URL 或 shell 参数作为服务器地址。"""

    version: Annotated[int, Meta(ge=0)] = field(default=0)
    source: Literal["aliyun", "tencent", "tuna", "custom"] = "aliyun"
    custom_host: str = ""
    custom_port: Annotated[int, Meta(ge=1, le=65535)] = field(default=123)
    threshold_ms: Annotated[int, Meta(ge=1, le=86400000)] = field(default=500)
    enabled: bool = False
    cron_expr: str = "0 * * * *"

    def validate_settings(self) -> "TimeCheckSettings":
        """禁用时也校验排程，使下次开启行为确定。"""
        if not self.cron_expr.strip() or not parse_cron_expr(self.cron_expr):
            raise ValueError("请设置有效的检查排程")
        host = self.custom_host.strip()
        if host:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                labels = host.rstrip(".").split(".")
                if len(host) > 253 or not all(
                    re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels
                ):
                    raise ValueError("自定义服务器必须是域名或 IP 地址") from None
        if self.source == "custom" and not host:
            raise ValueError("请输入自定义时间服务器")
        self.custom_host = host
        return self

    @property
    def host(self) -> str:
        """返回当前明确选中的源，不作隐式故障转移。"""
        return self.custom_host if self.source == "custom" else SOURCES[self.source]

    @property
    def port(self) -> int:
        """内置源固定使用标准 NTP 端口。"""
        return self.custom_port if self.source == "custom" else 123

    __forbid_unknown__ = True

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        self.validate_settings()
