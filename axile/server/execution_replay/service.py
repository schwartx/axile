"""原生回放读取入口；不连接账户、不恢复交易业务。"""

import json
from collections.abc import Mapping
from pathlib import Path

from axile.server.execution_replay.contracts import encode_json, freeze_json


def native_writer(root: str, account_id: int, recording_id: str, context_json: str):
    """延迟装载原生 writer，关闭录制时不创建文件或原生句柄。"""
    from axile.server._native import _core

    try:
        return _core.ExecutionReplayWriter(root, account_id, recording_id, context_json)
    except _core.NativeError as error:
        if error.args and error.args[0] == "REPLAY_BUSY":
            raise BlockingIOError("replay_writer_busy") from error
        raise


def _read(name: str, root: str | Path, account_id: int, execution_id: str, *args: str) -> dict[str, object]:
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
        raise ValueError("invalid_replay_account")
    if not execution_id or len(execution_id) > 256:
        raise ValueError("invalid_replay_execution")
    from axile.server._native import _core

    result = json.loads(getattr(_core, name)(str(root), account_id, execution_id, *args))
    if not isinstance(result, dict):
        raise ValueError("invalid_native_replay_response")
    return result


def replay_manifest(root: str | Path, account_id: int, execution_id: str) -> dict[str, object]:
    """读取已封口 recording 清单，不触发 seal 或账户操作。"""
    return _read("execution_replay_manifest", root, account_id, execution_id)


def replay_window(
    root: str | Path, account_id: int, execution_id: str, request: Mapping[str, object]
) -> dict[str, object]:
    """读取显式 recording 的固定窗口；最终预算与所有权由原生验证。"""
    raw = dict(request)
    cursor = raw.pop("cursor", None)
    if cursor is not None and (not isinstance(cursor, str) or len(cursor.encode("utf-8")) > 512 * 1024):
        raise ValueError("invalid_replay_cursor")
    frozen = freeze_json(raw)
    if not isinstance(frozen, dict) or not isinstance(frozen.get("recording_id"), str) or not frozen["recording_id"]:
        raise ValueError("replay_recording_required")
    if cursor is not None:
        frozen["cursor"] = cursor
    return _read("execution_replay_window", root, account_id, execution_id, encode_json(frozen))
