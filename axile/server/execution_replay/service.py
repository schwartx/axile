"""原生回放读取入口；不连接账户、不恢复交易业务。"""


def native_writer(root: str, account_id: int, recording_id: str, context_json: str):
    """延迟装载原生 writer，关闭录制时不创建文件或原生句柄。"""
    from axile.server._native import _core

    try:
        return _core.ExecutionReplayWriter(root, account_id, recording_id, context_json)
    except _core.NativeError as error:
        if error.args and error.args[0] == "REPLAY_BUSY":
            raise BlockingIOError("replay_writer_busy") from error
        raise
