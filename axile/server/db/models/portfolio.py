"""投资组合数据库模型."""

from typing import Optional

from pydantic import ConfigDict, Field, field_validator

from axile.server.db.models.base import Model, now_str


def _validate_custom_calc_py_code(value: str) -> str:
    """拒绝空白的组合计算函数，同时保留源码原始格式."""
    if not value.strip():
        raise ValueError("自定义组合计算函数不能为空")
    return value


class PortfolioBase(Model):
    """组合模型共用字段."""

    name: str = Field(description="投资组合名称, 必填")
    market: str = Field(description="交易市场标识, 例如: A股、期货等, 必填")
    description: Optional[str] = Field(default=None, description="投资组合的描述信息, 用于介绍组合逻辑或目的, 非必填")
    custom_calc_py_code: str = Field(
        description="定义 calculate_portfolio(context) 的 Python 源码；context 提供统一账户、持仓、行情和订单查询"
    )
    status: Optional[str] = Field(default=None, description="组合当前状态, 例如：启用、暂停、关闭, 非必填")
    tag: Optional[str] = Field(default=None, description="组合标签, 用于分类标记, 便于筛选和检索, 非必填")

    _validate_code = field_validator("custom_calc_py_code")(_validate_custom_calc_py_code)


class Portfolio(PortfolioBase):
    """由自定义函数计算目标权重的投资组合."""

    id: Optional[int] = Field(default=None)
    updated_at: str = Field(default_factory=now_str)
    created_at: str = Field(default_factory=now_str)


class PortfolioUpdate(Model):
    """组合的局部更新载荷."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    market: Optional[str] = None
    description: Optional[str] = None
    custom_calc_py_code: Optional[str] = None
    status: Optional[str] = None
    tag: Optional[str] = None

    @field_validator("custom_calc_py_code")
    @classmethod
    def validate_custom_calc_py_code(cls, value: str | None) -> str | None:
        """更新函数时拒绝空值和空白源码."""
        if value is None:
            raise ValueError("自定义组合计算函数不可清空")
        return _validate_custom_calc_py_code(value)


class PortfolioCreate(PortfolioBase):
    """创建组合时使用的载荷."""

    model_config = ConfigDict(extra="forbid")


class ValidateCustomCalcRequest(Model):
    """校验自定义组合脚本的请求载荷."""

    custom_calc_py_code: str
    account_id: Optional[int] = None


class RunAndSavePortfolioRequest(ValidateCustomCalcRequest):
    """试跑成功后保存代码和原始目标快照。"""

    name: str = Field(min_length=1)


class ValidateCustomCalcResponse(Model):
    """校验自定义组合脚本的响应载荷."""

    valid: bool
    target: Optional[dict[str, float]] = None
    error: Optional[str] = None
    traceback: Optional[str] = None
    error_line: Optional[int] = None
    error_offset: Optional[int] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None


class PortfolioLitePublic(PortfolioBase):
    """简化版组合信息, 不加载关系."""

    id: Optional[int]
    updated_at: str
    created_at: str


class PortfolioListPublic(Model):
    """轻量组合载荷的列表响应封装."""

    data: list[PortfolioLitePublic]
