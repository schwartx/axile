"""执行实时态广播中枢（``axile.server.execution.live``）测试。"""

import asyncio

from axile.domain.execution import ExecutionEventType
from axile.server.execution.live import ExecutionLiveHub


def test_subscribe_receives_frames() -> None:
    """订阅者应按事件收到广播帧，含账户、执行、阶段。"""

    async def scenario() -> list[dict[str, object]]:
        hub = ExecutionLiveHub()
        hub.bind_loop(asyncio.get_running_loop())
        received: list[dict[str, object]] = []

        async def consume() -> None:
            async with hub.subscribe() as queue:
                for _ in range(2):
                    received.append(await asyncio.wait_for(queue.get(), timeout=1.0))

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.02)  # 让订阅者先进入等待
        hub.publish(execution_id="e2", account_id=3, event_type=ExecutionEventType.EXECUTION_STARTED)
        hub.publish(execution_id="e2", account_id=3, event_type=ExecutionEventType.TARGET_COMPUTED)
        await task
        return received

    received = asyncio.run(scenario())
    assert [f["phase"] for f in received] == ["triggered", "planning"]
    assert all(f["account_id"] == 3 for f in received)
    assert all(f["execution_id"] == "e2" for f in received)
