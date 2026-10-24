"""业务事实的异常映射与审计引用身份校验。"""

from __future__ import annotations

from typing import NoReturn

REFERENCE_KEY = "business_facts_ref"


class BusinessFactReferenceError(ValueError):
    """审计引用缺失、越权或与冻结事实不一致。"""


def raise_business_fact_error(error: BaseException) -> NoReturn:
    """映射原生业务事实错误，其他错误保持原异常对象。"""
    if error.args[:1] == ("BUSINESS_FACTS",):
        raise ValueError(error.args[1]) from None
    if error.args[:1] == ("BUSINESS_FACTS_REFERENCE",):
        raise BusinessFactReferenceError(error.args[1]) from None
    raise error
