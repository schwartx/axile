"""隔离的原版回放界面验收服务，仅公开合成录制的只读接口。"""

import atexit
import json
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException

from axile.server._native import _core
from axile.server.api.routes import execution_replay as routes
from axile.server.execution_replay import reader

ROOT = Path(tempfile.mkdtemp(prefix="axile-replay-ui-"))
atexit.register(shutil.rmtree, ROOT, ignore_errors=True)
SYMBOL = "1000SHIBUSDT"
START = 1_794_816_000_000_000
context = {
    "schema_version": 2,
    "instrument": {SYMBOL: {"price_tick": 0.000001, "price_decimals": 6}},
    "channel": "paper",
}
writer = _core.ExecutionReplayWriter(str(ROOT), 7, "R", json.dumps(context))
events = [
    ("/execution", {"execution_id": "E", "phase": "started"}),
    (
        f"/quote/{SYMBOL}",
        {
            "symbol": SYMBOL,
            "bids": [[0.003876 - i * 0.000001, (i + 1) * 12000] for i in range(20)],
            "asks": [[0.003877 + i * 0.000001, (i + 1) * 9000] for i in range(20)],
            "book_valid": True,
            "valid_until_us": START + 30_000_000,
        },
    ),
    (
        "/order",
        {
            "symbol": SYMBOL,
            "order_id": "O1",
            "execution_id": "E",
            "direction": "BUY",
            "price": 0.003876,
            "quantity": 250,
            "filled_quantity": 0,
            "remaining_quantity": 250,
            "confirmed_book": True,
            "sync_generation": 1,
            "status": "working",
        },
    ),
    (
        f"/market_trade/{SYMBOL}",
        {
            "symbol": SYMBOL,
            "trade_key": "M1",
            "identity_quality": "stable",
            "price": 0.003877,
            "quantity": 30000,
            "direction": "BUY",
        },
    ),
    (
        "/trade",
        {
            "symbol": SYMBOL,
            "execution_id": "E",
            "order_id": "O1",
            "trade_key": "T1",
            "identity_quality": "stable",
            "price": 0.003876,
            "quantity": 25,
            "direction": "BUY",
        },
    ),
    (
        f"/quote/{SYMBOL}",
        {
            "symbol": SYMBOL,
            "bids": [[0.003877 - i * 0.000001, (i + 1) * 15000] for i in range(20)],
            "asks": [[0.003878 + i * 0.000001, (i + 1) * 11000] for i in range(20)],
            "book_valid": True,
            "valid_until_us": START + 30_000_000,
        },
    ),
    (
        "/order",
        {
            "symbol": SYMBOL,
            "order_id": "O1",
            "execution_id": "E",
            "direction": "BUY",
            "price": 0.003876,
            "quantity": 250,
            "filled_quantity": 25,
            "remaining_quantity": 225,
            "confirmed_book": True,
            "sync_generation": 1,
            "status": "working",
        },
    ),
    ("/execution", {"execution_id": "E", "phase": "finished"}),
]
for sequence, (topic, data) in enumerate(events, 1):
    writer.append(
        json.dumps(
            {
                "schema_version": 2,
                "recording_id": "R",
                "capture_seq": sequence,
                "time_us": START + (sequence - 1) * 1_000_000,
                "topic": topic,
                "data": data,
            }
        )
    )
writer.finish()
bulk = _core.ExecutionReplayWriter(str(ROOT), 7, "R-bulk", json.dumps(context))
for sequence in range(1, 141):
    symbol = SYMBOL if sequence % 2 else "ZZZ"
    bulk.append(
        json.dumps(
            {
                "schema_version": 2,
                "recording_id": "R-bulk",
                "capture_seq": sequence,
                "time_us": START + sequence * 1000,
                "topic": f"/quote/{symbol}",
                "data": {
                    "symbol": symbol,
                    "execution_id": "E-bulk",
                    "bids": [[0.003876, 250]],
                    "asks": [[0.003877, 100]],
                    "book_valid": True,
                    "valid_until_us": START + 30_000_000,
                },
            }
        )
    )
    bulk.seal()
bulk.finish()
clock = [0.0]
reader.readers = reader.Readers(lambda: clock[0])
reader.readers.open(7, "E", ROOT)
clock[0] = 20.0


async def execution_account(execution_id: str) -> int:
    if execution_id not in {"E", "E-bulk"}:
        raise HTTPException(404, "execution not found")
    return 7


routes._execution_account = execution_account
routes.replay_root = lambda: ROOT
app = FastAPI()
app.include_router(routes.router, prefix="/api/v1/account")


@app.get("/health")
def health():
    return {"ok": True}
