"""通过完整原生读操作获取冻结业务事实。"""

from collections.abc import Mapping
from typing import Any

from axile.server._native import _core
from axile.server.business_facts.summary_refs import raise_business_fact_error
from axile.server.native_store import native_operation


async def _read(name: str, request: Mapping[str, object]) -> Any:
    try:
        return await native_operation(name, request)
    except _core.NativeError as error:
        raise_business_fact_error(error)


def _record_identity(record: Any) -> dict[str, object]:
    if isinstance(record, Mapping):
        record_id, account_id = record.get("id"), record.get("account_id")
    else:
        record_id, account_id = record.id, record.account_id
    if type(record_id) is not int or type(account_id) is not int:
        raise ValueError("执行记录缺少主键或账户身份")
    return {"record_id": record_id, "account_id": account_id}


async def read_execution_input(input_id: str) -> object:
    """显式读取已冻结输入，缺失关联事实视为完整性错误。"""
    return await _read("read_execution_input", {"input_id": input_id})


async def read_execution_result(record: Any) -> object:
    """读取完整结果并验证输入、资产及通知引用的归属和完整性。"""
    return await _read("read_execution_result", _record_identity(record))


async def read_execution_detail(record: Any) -> dict[str, object]:
    """在同一数据库快照中读取执行标量、输入和完整结果。"""
    return await _read("read_execution_detail", _record_identity(record))
