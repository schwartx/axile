"""投资组合数据库模型."""

from dataclasses import dataclass, field
from math import isfinite
from typing import Annotated, Optional

from msgspec import UNSET, Meta, UnsetType

from axile.common.serialization import provided_fields
from axile.server.db.models.base import now_str


def _validate_custom_calc_py_code(value: str) -> str:
    """拒绝空白的组合计算函数，同时保留源码原始格式."""
    if not value.strip():
        raise ValueError("自定义组合计算函数不能为空")
    return value


@dataclass(kw_only=True)
class PortfolioBase:
    """组合模型共用字段."""

    name: Annotated[str, Meta(description="投资组合名称, 必填")]
    market: Annotated[str, Meta(description="交易市场标识, 例如: A股、期货等, 必填")]
    description: Annotated[Optional[str], Meta(description="投资组合的描述信息, 用于介绍组合逻辑或目的, 非必填")] = (
        field(default=None)
    )
    custom_calc_py_code: Annotated[
        str,
        Meta(
            description="定义 calculate_portfolio(context) 的 Python 源码；context 提供统一账户、持仓、行情和订单查询"
        ),
    ]
    status: Annotated[Optional[str], Meta(description="组合当前状态, 例如：启用、暂停、关闭, 非必填")] = field(
        default=None
    )
    tag: Annotated[Optional[str], Meta(description="组合标签, 用于分类标记, 便于筛选和检索, 非必填")] = field(
        default=None
    )

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        if "custom_calc_py_code" in provided_fields(self):
            object.__setattr__(self, "custom_calc_py_code", _validate_custom_calc_py_code(self.custom_calc_py_code))


@dataclass(kw_only=True)
class Portfolio(PortfolioBase):
    """由自定义函数计算目标权重的投资组合."""

    id: Optional[int] = field(default=None)
    archived_at: str | None = None
    updated_at: str = field(default_factory=now_str)
    created_at: str = field(default_factory=now_str)

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        super().validate()


@dataclass(kw_only=True)
class PortfolioUpdate:
    """组合的局部更新载荷."""

    name: Optional[str] | UnsetType = UNSET
    market: Optional[str] | UnsetType = UNSET
    description: Optional[str] | UnsetType = UNSET
    custom_calc_py_code: Optional[str] | UnsetType = UNSET
    status: Optional[str] | UnsetType = UNSET
    tag: Optional[str] | UnsetType = UNSET

    @classmethod
    def validate_required_text(cls, value: str | None) -> str:
        """更新名称和交易市场时拒绝空值；省略字段保持原值。"""
        if value is None:
            raise ValueError("组合名称和交易市场不可清空")
        return value

    @classmethod
    def validate_custom_calc_py_code(cls, value: str | None) -> str | None:
        """更新函数时拒绝空值和空白源码."""
        if value is None:
            raise ValueError("自定义组合计算函数不可清空")
        return _validate_custom_calc_py_code(value)

    __forbid_unknown__ = True

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        if "name" in provided_fields(self):
            if self.name is None:
                raise ValueError("组合名称和交易市场不可清空 - at `$.name`")
            object.__setattr__(self, "name", self.validate_required_text(self.name))
        if "market" in provided_fields(self):
            if self.market is None:
                raise ValueError("组合名称和交易市场不可清空 - at `$.market`")
            object.__setattr__(self, "market", self.validate_required_text(self.market))
        if "custom_calc_py_code" in provided_fields(self):
            object.__setattr__(self, "custom_calc_py_code", self.validate_custom_calc_py_code(self.custom_calc_py_code))


@dataclass(kw_only=True)
class PortfolioCreate(PortfolioBase):
    """创建组合时使用的载荷."""

    __forbid_unknown__ = True

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        super().validate()


@dataclass(kw_only=True)
class TrialLeverageOverrides:
    """仅本次试跑使用的杠杆覆盖值，不写入账户。"""

    long_leverage: Annotated[float, Meta(ge=0)] | None = field(default=None)
    short_leverage: Annotated[float, Meta(ge=0)] | None = field(default=None)

    __forbid_unknown__ = True

    def validate(self) -> None:
        """在输入入口检查本对象的业务约束。"""
        if (
            "long_leverage" in provided_fields(self)
            and self.long_leverage is not None
            and not isfinite(self.long_leverage)
        ):
            raise ValueError("long_leverage 必须是有限数值")
        if (
            "short_leverage" in provided_fields(self)
            and self.short_leverage is not None
            and not isfinite(self.short_leverage)
        ):
            raise ValueError("short_leverage 必须是有限数值")


@dataclass(kw_only=True)
class ValidateCustomCalcRequest:
    """校验自定义组合脚本的请求载荷."""

    custom_calc_py_code: str
    account_id: Optional[int] = None
    leverage_overrides: TrialLeverageOverrides | None = None


@dataclass(kw_only=True)
class RunAndSavePortfolioRequest:
    """试跑成功后保存代码和原始目标快照。"""

    name: Annotated[str, Meta(min_length=1)]
    custom_calc_py_code: str
    account_id: Optional[int] = None
