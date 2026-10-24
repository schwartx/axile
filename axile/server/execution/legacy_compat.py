"""旧结果的 Python 调用接口；展示归一化由 Rust 统一维护。"""

import json

from axile.server._native import _core


def normalize_legacy_result(raw: object) -> object:
    """补齐旧结果的展示字段；非字典输入原样保留，不写入数据库。"""
    if not isinstance(raw, dict):
        return raw
    return _core.normalize_execution_result(json.dumps(raw, ensure_ascii=False))
