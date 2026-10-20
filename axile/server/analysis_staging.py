"""将只读 Rust 的有界结果批次暂存到磁盘，再交给 Python 原子发布。"""

from __future__ import annotations

import json
import math
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

import sqlalchemy as sa

from axile.server.db.models.analysis_result import RESULT_TABLES
from axile.server.native_analysis import NativeAnalysisProtocolError

_TABLES = {table.name: table for table in RESULT_TABLES}


def _invalid(message: str) -> NativeAnalysisProtocolError:
    return NativeAnalysisProtocolError(message, code="INVALID_NATIVE_PUBLICATION")


@dataclass(slots=True)
class StagedPublication:
    """仅保留小型完成元数据；逐批结果由临时文件持有。"""

    file: BinaryIO
    complete: dict

    def rows(self) -> Iterator[tuple[str, list[str], list[list]]]:
        """逐帧重读已校验批次，不构造完整分析对象树。"""
        self.file.seek(0)
        for line in self.file:
            frame = json.loads(line)
            yield frame["table"], frame["columns"], frame["rows"]

    def close(self) -> None:
        """关闭并移除临时文件，取消或版本过期时同样调用。"""
        self.file.close()


def _validate_rows(frame: dict) -> tuple[str, list[str], list[list]]:
    name = frame.get("table")
    if not isinstance(name, str) or name not in _TABLES:
        raise _invalid("原生结果包含未知表")
    columns, rows = frame.get("columns"), frame.get("rows")
    expected = {column.name for column in _TABLES[name].columns if column.name != "snapshot_id"}
    if (
        not isinstance(columns, list)
        or not all(isinstance(column, str) for column in columns)
        or len(columns) != len(set(columns))
        or set(columns) != expected
    ):
        raise _invalid("原生结果字段与结构化表不匹配")
    if not isinstance(rows, list) or not 0 < len(rows) <= 256:
        raise _invalid("原生结果批次行数不合法")
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise _invalid("原生结果行宽不匹配")
        for column, value in zip(columns, row, strict=True):
            _validate_cell(_TABLES[name].c[column], value)
        if row[columns.index("range_key")] not in {"30", "90", "all"}:
            raise _invalid("原生结果包含未知区间")
    return name, columns, rows


def _validate_cell(column, value) -> None:
    """在 SQLite 写入前拒绝隐式文本、布尔和金额类型转换。"""
    if value is None:
        if not column.nullable:
            raise _invalid("非空结果字段缺少值")
        return
    kind = column.type
    if isinstance(kind, sa.Boolean):
        valid = type(value) is bool
    elif isinstance(kind, sa.Integer):
        valid = type(value) is int and -(2**63) <= value < 2**63
    elif isinstance(kind, sa.Float):
        valid = type(value) in (int, float) and math.isfinite(value)
    elif isinstance(kind, sa.Text):
        valid = isinstance(value, str)
    else:
        valid = False
    if not valid:
        raise _invalid("结构化结果单元格类型不匹配")


def _validate_complete(frame: dict, counts: Counter, account_id: int, source_version: int, ranges: set[str]) -> None:
    if type(frame.get("account_id")) is not int or frame["account_id"] != account_id:
        raise _invalid("原生结果账户不匹配")
    if type(frame.get("source_version")) is not int or frame["source_version"] != source_version:
        raise _invalid("原生结果源版本不匹配")
    actual = frame.get("row_counts")
    if not isinstance(actual, dict) or any(type(value) is not int or value < 0 for value in actual.values()):
        raise _invalid("原生结果缺少有效批次计数")
    if {name: count for name, count in actual.items() if count} != dict(counts):
        raise _invalid("原生结果批次计数不完整")
    if ranges != {"30", "90", "all"} or counts["performance_result_range"] != 3:
        raise _invalid("原生结果必须包含三个完整区间")
    if frame.get("data_until") is not None and not isinstance(frame["data_until"], str):
        raise _invalid("原生结果数据时间不合法")


async def stage_publication(
    native, *, account_id: int, source_version: int, settings: dict, trade_channel: str
) -> StagedPublication:
    """成功收齐并验证全部批次后返回；任何异常均关闭磁盘暂存。"""
    # TemporaryFile 不保留可见路径；不因历史长度而增长 Python 结果列表。
    spool = tempfile.TemporaryFile(mode="w+b")
    complete = None
    counts: Counter[str] = Counter()
    ranges: set[str] = set()
    try:
        async with native.query_stream(
            "publish",
            account_id=account_id,
            expected_source_version=source_version,
            settings=settings,
            trade_channel=trade_channel,
            include_metadata=True,
        ) as frames:
            async for frame in frames:
                if not isinstance(frame, dict) or complete is not None:
                    raise _invalid("原生结果包含完成标记后的数据")
                if frame.get("type") == "complete":
                    _validate_complete(frame, counts, account_id, source_version, ranges)
                    complete = frame
                    continue
                if frame.get("type") != "rows":
                    raise _invalid("原生结果帧类型不合法")
                name, columns, rows = _validate_rows(frame)
                counts[name] += len(rows)
                if name == "performance_result_range":
                    index = columns.index("range_key")
                    ranges.update(row[index] for row in rows)
                spool.write(json.dumps(frame, allow_nan=False, separators=(",", ":")).encode() + b"\n")
        if complete is None:
            raise _invalid("原生结果未完成，不能发布")
        spool.seek(0)
        return StagedPublication(spool, complete)
    except BaseException:
        spool.close()
        raise
