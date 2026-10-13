"""交易渠道可用性查询路由."""

from fastapi import APIRouter
from pydantic import BaseModel

from axile.channels import (
    ChannelAccountForm,
    ChannelCalendar,
    ChannelDefaults,
    ChannelLeverage,
    ChannelPortfolioPreset,
    ChannelSchedule,
    ChannelUi,
    ChannelUnits,
    list_channels,
)
from axile.channels.contracts import ChannelConnectionValidation
from axile.server.channel_capabilities import install_extra, missing_packages

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


class ChannelCapabilityPublic(BaseModel):
    """
    单个交易渠道的依赖可用性.

    Attributes
    ----------
    channel : str
        交易渠道标识。
    available : bool
        该渠道可选依赖是否已全部安装。
    missing_packages : list[str]
        尚未安装的第三方包名；``available`` 为 ``True`` 时为空列表。
    install_extra : str
        安装该渠道依赖所用的 pyproject extra 名（``uv sync --extra <extra>``）。
    """

    channel: str
    label: str
    description: str
    icon: str
    market: str
    currency: str
    units: ChannelUnits
    ui: ChannelUi
    defaults: ChannelDefaults
    leverage: ChannelLeverage
    account_form: ChannelAccountForm
    calendar: ChannelCalendar | None
    schedule: ChannelSchedule
    portfolio: ChannelPortfolioPreset
    connection_validation: ChannelConnectionValidation | None = None
    available: bool
    missing_packages: list[str]
    install_extra: str | None


@router.get("/channels", response_model=list[ChannelCapabilityPublic])
def list_channel_capabilities() -> list[ChannelCapabilityPublic]:
    """
    返回各交易渠道的依赖可用性.

    Returns
    -------
    list[ChannelCapabilityPublic]
        按注册表稳定顺序返回当前进程发现的全部渠道。

    Notes
    -----
    仅探测本机是否安装了对应依赖（``importlib.util.find_spec``），不判断平台支持；
    渠道名称、图标、账户表单与默认参数均由插件描述对象下发，前端按结构化数据渲染。
    """
    return [
        ChannelCapabilityPublic(
            **plugin.descriptor.model_dump(mode="python"),
            available=not (missing := missing_packages(plugin.descriptor.channel)),
            missing_packages=missing,
            install_extra=install_extra(plugin.descriptor.channel),
        )
        for plugin in list_channels()
    ]
