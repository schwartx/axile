"""记录原生报单调用事实；与发送前的风控额度用量独立。"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from threading import RLock
from typing import Literal
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel

from axile.executor.algorithms.utils.clock import clock_now_ms


class OrderSubmissionFact(BaseModel):
    """一次原生调用的身份及结果；未知结果不能推断为柜台已受理。"""

    submission_uid: str
    account_id: int
    channel: str = "ctp"
    source: str
    operation: Literal["place_order", "cancel_order"] = "place_order"
    execution_id: str | None = None
    symbol: str
    order_id: str
    request_id: int
    occurred_at_ms: int
    revision: int = 0
    send_status: Literal["pending", "accepted", "rejected", "unknown"] = "pending"
    return_code: int | None = None
    broker_status: Literal["unknown", "acknowledged", "rejected"] = "unknown"
    error_id: int | None = None


class OrderSubmissionRecorder:
    """主程序和直连脚本共用的原生调用记录器。

    Notes
    -----
    sink 须按 submission_uid 幂等更新、按 revision 拒绝旧版本覆盖。
    调用前必须写入待确认事实；调用后的存储故障不改变下单返回值，避免误触重试。
    仅保留最近 4096 次调用的回调关联，持久化事实由 sink 保留。
    """

    def __init__(self, account_id: int, sink: Callable[[OrderSubmissionFact], None], *, source: str = "script") -> None:
        self.account_id = account_id
        self.source = source
        self._sink = sink
        self._lock = RLock()
        self._requests: OrderedDict[int, OrderSubmissionFact] = OrderedDict()
        self._orders: dict[str, int] = {}

    def submit(
        self,
        call: Callable[[], int],
        *,
        symbol: str,
        order_id: str,
        request_id: int,
        execution_id: str | None = None,
        source: str | None = None,
        operation: Literal["place_order", "cancel_order"] = "place_order",
    ) -> int:
        """在本地校验之后包装一次原生报单，记录同步返回并保留提前到达的回报。"""
        fact = OrderSubmissionFact(
            submission_uid=uuid4().hex,
            account_id=self.account_id,
            source=source or self.source,
            operation=operation,
            execution_id=execution_id,
            symbol=symbol,
            order_id=order_id,
            request_id=request_id,
            occurred_at_ms=clock_now_ms(),
        )
        with self._lock:
            if request_id in self._requests:
                raise ValueError("同一记录器不能重复使用原生 request_id")
            self._sink(fact)
            # 初始待确认事实先落盘；已知发送结果采用紧邻原生调用的时间，避免写盘跨日误归桶。
            fact = fact.model_copy(update={"occurred_at_ms": clock_now_ms()})
            self._requests[request_id] = fact
            if operation == "place_order":
                self._orders[order_id] = request_id
            while len(self._requests) > 4096:
                _, old = self._requests.popitem(last=False)
                if self._orders.get(old.order_id) == old.request_id:
                    self._orders.pop(old.order_id, None)
        try:
            code = int(call())
        except BaseException:
            self._update(request_id, send_status="unknown")
            raise
        self._update(request_id, send_status="accepted" if code == 0 else "rejected", return_code=code)
        return code

    def broker_result(self, order_id: str, *, rejected: bool, error_id: int | None = None) -> None:
        """按完整订单身份关联回报，不把查询到的其他来源委托补算为本地发送。"""
        with self._lock:
            request_id = self._orders.get(order_id)
        if request_id is not None:
            self._update(request_id, broker_status="rejected" if rejected else "acknowledged", error_id=error_id)

    def _update(self, request_id: int, **changes: object) -> None:
        with self._lock:
            fact = self._requests.get(request_id)
            if fact is None:
                # 极迟到的调用结果超过关联窗口时保留持久化待确认事实，不改变交易结果。
                return
            if fact.broker_status == "rejected" and changes.get("broker_status") == "acknowledged":
                return
            updated = fact.model_copy(update={**changes, "revision": fact.revision + 1})
            self._requests[request_id] = updated
            try:
                self._sink(updated)
            except Exception:
                logger.exception("报单事实更新失败，保留待确认记录 submission_uid={}", fact.submission_uid)
