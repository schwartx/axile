"""执行器的可选只读观察出口，不依赖服务端或原生存储。"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import NamedTuple
from uuid import uuid4

ReplayObserver = Callable[[str, Mapping[str, object]], None]
_MISSING = object()


class ReplayReceipt(NamedTuple):
    """消息或操作入口固定的录制身份。"""

    observer: ReplayObserver
    sync_generation: int | None


_command_context: ContextVar[tuple[str | None, ReplayReceipt | None] | None] = ContextVar(
    "replay_command_context", default=None
)


def capture_receipt(owner: object | None) -> ReplayReceipt | None:
    """固定观察者和代次，后续阶段不借用新的录制身份。"""
    observer = getattr(owner, "_replay_observer", None)
    if observer is None:
        return None
    generation = getattr(owner, "_replay_sync_generation", None)
    return ReplayReceipt(observer, generation if type(generation) is int else None)


def observe_receipt(receipt: ReplayReceipt | None, topic: str, data: Mapping[str, object]) -> None:
    """沿入口身份交付观察，未知代次不在交付时补造。"""
    if receipt is None:
        return
    try:
        receipt.observer(topic, {**data, "sync_generation": receipt.sync_generation})
    except Exception:  # noqa: BLE001 - 录制旁路不能传播到交易路径
        return


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
    context = _command_context.get()
    return context[0] if context is not None else None


def capture_command_receipt(owner: object | None) -> ReplayReceipt | None:
    """沿用公共报撤入口身份；直接调用渠道时才现场固定身份。"""
    context = _command_context.get()
    return context[1] if context is not None else capture_receipt(owner)


@contextmanager
def replay_command(
    owner: object, operation: str, data: Mapping[str, object], *, receipt: object = _MISSING
) -> Iterator[str | None]:
    """覆盖报撤调用及装饰器拒绝，异常原样传播且不保存异常文本。"""
    if receipt is _MISSING:
        receipt = capture_receipt(owner)
    receipt = receipt if isinstance(receipt, ReplayReceipt) else None
    identity = uuid4().hex if receipt is not None else None
    token = _command_context.set((identity, receipt))
    try:
        if receipt is None:
            yield None
            return
        base = {**data, "command_id": identity, "operation": operation}
        observe_receipt(receipt, "/command", {**base, "phase": "request"})
        try:
            yield identity
        except BaseException as error:
            observe_receipt(receipt, "/command", {**base, "phase": "error", "error_type": type(error).__name__})
            raise
        else:
            observe_receipt(receipt, "/command", {**base, "phase": "returned"})
    finally:
        _command_context.reset(token)
