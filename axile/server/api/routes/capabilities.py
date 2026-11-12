"""交易渠道可用性查询路由."""

from typing import Any

from fastapi import APIRouter

from axile.channels import (
    list_channels,
)
from axile.executor.connection_progress import connection_plan
from axile.server.channel_capabilities import install_extra, missing_packages

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


@router.get("/channels", response_model=None)
def list_channel_capabilities() -> list[dict[str, Any]]:
    """
    返回各交易渠道的依赖可用性.

    Returns
    -------
    list[dict[str, Any]]
        按注册表稳定顺序返回当前进程发现的全部渠道。

    Notes
    -----
    仅探测本机是否安装了对应依赖（``importlib.util.find_spec``），不判断平台支持；
    渠道名称、图标、账户表单与默认参数均由插件描述对象下发，前端按结构化数据渲染。
    """
    result = []
    for plugin in list_channels():
        descriptor = plugin.descriptor
        public = descriptor.model_dump(
            mode="json",
            include={
                "futures_close",
                "channel",
                "label",
                "description",
                "icon",
                "market",
                "currency",
                "units",
                "ui",
                "replay",
                "defaults",
                "leverage",
                "account_form",
                "calendar",
                "schedule",
                "portfolio",
                "connection_steps",
                "connection_validation",
            },
        )
        if descriptor.connection_validation is not None:
            public["connection_validation"]["steps"] = [
                step.model_dump(mode="json") for step in connection_plan(descriptor.channel, "validation")
            ]
        missing = missing_packages(descriptor.channel)
        public.update(available=not missing, missing_packages=missing, install_extra=install_extra(descriptor.channel))
        result.append(public)
    return result
