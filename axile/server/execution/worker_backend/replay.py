"""账户录制的配置和连接桥接；所有失败均与交易结果隔离。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axile.channels import get_channel
from axile.server.db.models.account import AccountContext
from axile.server.execution.worker_backend.protocol import WorkerReplayConfig


def replay_root() -> Path:
    """以服务端配置的数据库目录确定录制根目录。"""
    from axile.server.core.db import database_path

    return Path(database_path()).expanduser().resolve().parent / "replay"


def replay_config(account: AccountContext) -> WorkerReplayConfig:
    """只传渠道声明和录制意愿，不将账户配置交给录制器。"""
    context: dict[str, object] = {"channel": str(account.trade_channel)}
    supported = False
    try:
        descriptor = get_channel(str(account.trade_channel)).descriptor
        declaration = getattr(descriptor, "replay", None)
        if declaration is not None:
            context.update(declaration.model_dump(mode="json"))
            context["stream_defaults"] = context.pop("streams", {})
            supported = declaration.supported
        context["units"] = descriptor.units.model_dump(mode="json")
        context["currency"] = descriptor.currency
    except (KeyError, RuntimeError):
        # 历史读取及开关状态不要求当前安装对应渠道插件。
        pass
    return WorkerReplayConfig(
        enabled=account.replay_recording_enabled,
        config_version=account.replay_config_version,
        root=str(replay_root()),
        context=context,
        supported=supported,
    )


def worker_replay_status(state: Any) -> dict[str, object]:
    """读取录制状态，不访问连接或文件系统。"""
    config = state.replay_config
    if state.replay_recorder is not None and state.replay_error is None:
        try:
            return state.replay_recorder.status()
        except Exception:
            state.replay_error = "录制状态读取失败，交易继续运行"
    desired = config.enabled if config is not None else False
    status = "error" if state.replay_error else "disabled"
    if desired and not state.replay_error:
        status = "waiting_connection" if config.supported else "unsupported"
    return {
        "desired": desired,
        "state": status,
        "config_version": config.config_version if config is not None else 0,
        "effective_version": config.config_version
        if config is not None and not desired and not state.replay_error
        else 0,
        "recording_id": None,
        "error": state.replay_error,
    }


def sync_worker_replay_connection(state: Any, ready: bool | None = None) -> None:
    """只绑定已有连接；控制线程无需等待交易资源锁。"""
    with state.replay_lock:
        if state.replay_recorder is None or state.replay_config is None:
            return
        try:
            connected = state.connected_once and not state.disconnected if ready is None else ready
            state.replay_recorder.attach(
                state.executor if connected else None,
                state.replay_config.context,
                supported=state.replay_config.supported,
            )
        except Exception:
            state.replay_error = "录制连接观察失败，交易继续运行"


def configure_worker_replay(state: Any, config: WorkerReplayConfig) -> dict[str, object]:
    """按版本请求启停，创建和排空文件均由录制器后台负责。"""
    with state.replay_lock:
        previous = state.replay_config
        if previous is not None and config.config_version <= previous.config_version:
            return worker_replay_status(state)
        state.replay_config = config
        state.replay_error = None
        try:
            if state.replay_recorder is None and config.enabled:
                from axile.server.execution_replay.recorder import ReplayRecorder

                state.replay_recorder = ReplayRecorder(state.account_id, config.root, config.context)
                if state.replay_execution_id is not None:
                    state.replay_recorder.begin_execution(state.replay_execution_id, state.replay_execution_symbols)
            if state.replay_recorder is not None:
                state.replay_recorder.configure(config.enabled, config.config_version)
                sync_worker_replay_connection(state)
        except Exception:
            state.replay_error = "录制配置未生效，交易继续运行"
        return worker_replay_status(state)


def begin_worker_replay_execution(state: Any, execution_id: str | None, symbols: tuple[str, ...]) -> None:
    """登记当前执行，让半途启用从已知执行上下文开始。"""
    with state.replay_lock:
        state.replay_execution_id = execution_id
        state.replay_execution_symbols = symbols
        if execution_id is not None and state.replay_recorder is not None:
            try:
                state.replay_recorder.begin_execution(execution_id, symbols)
            except Exception:
                state.replay_error = "执行录制开始观察失败，交易继续运行"


def end_worker_replay_execution(state: Any, reason: str) -> None:
    """执行结束仅封口和启动尾部，不卸载账户回报观察。"""
    with state.replay_lock:
        execution_id = state.replay_execution_id
        state.replay_execution_id, state.replay_execution_symbols = None, ()
        if execution_id is not None and state.replay_recorder is not None:
            try:
                state.replay_recorder.end_execution(execution_id, {"ending_reason": reason})
            except Exception:
                state.replay_error = "执行录制结束观察失败，交易继续运行"


def close_worker_replay(state: Any) -> None:
    """退出时有界收尾，不让录制错误覆盖交易或连接结果。"""
    if state.replay_recorder is not None:
        try:
            state.replay_recorder.close(timeout=1.0)
        except Exception:
            state.replay_error = "录制收尾失败"
