"""浏览器编辑器测试专用服务：不启动数据库、账户或交易调度器."""

from fastapi import FastAPI

from axile.server.api.routes.editor import _sessions, router
from tests.orders_app import app as orders_app
from tests.orders_app import lifespan

app = FastAPI(lifespan=lifespan)
app.include_router(orders_app.router)
app.include_router(router, prefix="/api/v1")


@app.post("/test/disconnect")
async def disconnect():
    """模拟语言服务连接丢失."""
    for socket in tuple(_sessions):
        await socket.close(code=1012)
    return {"ok": True}
