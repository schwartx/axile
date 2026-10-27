"""CLI 在仍有 SSE 客户端时也应完成关停清理。"""

import http.client
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="使用 POSIX SIGTERM 验证真实停服")
def test_sigterm_closes_open_stream_and_finishes_lifespan(tmp_path: Path) -> None:
    """保持 SSE 连接打开，确保 SIGTERM 限时退出且执行 lifespan 清理。"""
    script = r"""
import asyncio,sys
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from axile.server import main

@asynccontextmanager
async def lifespan(app):
    yield
    Path(sys.argv[2]).write_text('closed')

app=FastAPI(lifespan=lifespan)
@app.get('/stream')
async def stream():
    async def events():
        yield b'data: ready\n\n'
        await asyncio.Event().wait()
    return StreamingResponse(events(),media_type='text/event-stream')

main._app=app
main.is_configured=lambda:False
main.setup_logging=lambda:None
main.shutdown_logging=lambda:None
main.run_server(host='127.0.0.1',port=int(sys.argv[1]),workers=1)
"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    closed = tmp_path / "lifespan-closed"
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(port), str(closed)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                connection.request("GET", "/stream")
                response = connection.getresponse()
                break
            except OSError:
                connection.close()
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("测试服务未及时启动") from None
                time.sleep(0.05)
        assert response.status == 200
        assert response.readline() == b"data: ready\n"
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            pytest.fail("SSE 仍打开时 SIGTERM 未在 8 秒内完成停服")
        assert time.monotonic() - started < 8
        assert process.returncode == 0, output[-2000:]
        assert closed.read_text() == "closed"
    finally:
        connection.close()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
