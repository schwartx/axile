"""执行器的可选只读观察出口，不依赖服务端或原生存储。"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

ReplayObserver = Callable[[str, Mapping[str, object]], None]
_command_id: ContextVar[str | None] = ContextVar("replay_command_id", default=None)


def set_replay_observer(owner: object, callback: ReplayObserver | None) -> None:
    """绑定账户观察者；空闲执行器不创建后台资源。"""
    setattr(owner, "_replay_observer", callback)


def replay_enabled(owner: object) -> bool:
    """允许渠道在构造观察数据前进行轻量开关检查。"""
    return getattr(owner, "_replay_observer", None) is not None


def begin_query(owner: object, scope: list[str]) -> dict[str, object] | None:
    """在正常查询开始前取得本地覆盖身份，不执行查询或分配交易请求。"""
    if not replay_enabled(owner):
        return None
    recorder = getattr(owner, "_replay_recorder", None)
    if recorder is None:
        return None
    try:
        return recorder.begin_query(scope)
    except Exception:  # noqa: BLE001 - 无可确认覆盖时由集合投影保留未知
        return None


def finish_query(owner: object, token: dict[str, object] | None) -> None:
    """结束查询并释放未交付的覆盖凭据，不改变查询结果。"""
    recorder = getattr(owner, "_replay_recorder", None)
    if recorder is None or token is None:
        return
    try:
        recorder.finish_query(token)
    except Exception:  # noqa: BLE001 - 回收旁路失败不能覆盖业务查询的结果或异常
        return


def observe(owner: object, topic: str, data: Mapping[str, object]) -> None:
    """交付已规范化的允许字段，观察故障不能改变交易结果。"""
    callback = getattr(owner, "_replay_observer", None)
    if callback is None:
        return
    try:
        callback(topic, data)
    except Exception:  # noqa: BLE001 - 录制旁路不能传播到交易路径
        return


def current_command_id() -> str | None:
    """返回当前同步报撤调用的观察身份。"""
    return _command_id.get()


@contextmanager
def replay_command(owner: object, operation: str, data: Mapping[str, object]) -> Iterator[str | None]:
    """覆盖报撤调用及装饰器拒绝，异常原样传播且不保存异常文本。"""
    if getattr(owner, "_replay_observer", None) is None:
        yield None
        return
    identity = uuid4().hex
    token = _command_id.set(identity)
    base = {**data, "command_id": identity, "operation": operation}
    observe(owner, "/command", {**base, "phase": "request"})
    try:
        yield identity
    except BaseException as error:
        observe(owner, "/command", {**base, "phase": "error", "error_type": type(error).__name__})
        raise
    else:
        observe(owner, "/command", {**base, "phase": "returned"})
    finally:
        _command_id.reset(token)
