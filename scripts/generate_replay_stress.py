"""Generate deterministic v2 stress evidence; these are synthetic identities, not real trades."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from axile.server._native import _core


def generate(
    root: Path, account: int, recording: str, identities: int, proofs: int, interleaved: bool, symbols: int = 1
):
    """Write raw evidence once using the production writer, with independently known scalar totals."""
    if not 1 <= identities <= 40_000 or not 1 <= proofs <= 256 or not 1 <= symbols <= 24:
        raise ValueError("stress_input_budget")
    writer = _core.ExecutionReplayWriter(str(root), account, recording, '{"schema_version":2}')
    sequence = 0

    def emit(topic, data):
        nonlocal sequence
        sequence += 1
        writer.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "recording_id": recording,
                    "capture_seq": sequence,
                    "time_us": sequence * 1000,
                    "topic": topic,
                    "data": data,
                },
                separators=(",", ":"),
            )
        )

    names = ["S"] if symbols == 1 else [f"S{i:02d}" for i in range(symbols)]
    emit("/execution", {"execution_id": "scale-E", "phase": "start", "symbols": names})
    for symbol in names:
        emit(f"/quote/{symbol}", {"symbol": symbol, "book_valid": True, "bids": [[10, 1]], "asks": [[11, 1]]})
    for i in range(identities):
        symbol = names[i % symbols]
        emit(
            "/command",
            {
                "command_id": f"C{i}",
                "operation": "place_order",
                "symbol": symbol,
                "order_id": f"O{i}",
                "execution_id": "scale-E",
                "phase": "returned",
            },
        )
        emit(
            "/order",
            {
                "symbol": symbol,
                "order_id": f"O{i}",
                "quantity": 2,
                "filled_quantity": 0,
                "status": "new",
                "sync_generation": 1,
            },
        )
        emit(
            "/trade",
            {
                "symbol": symbol,
                "order_id": f"O{i}",
                "trade_key": f"T{i:06d}",
                "identity_quality": "stable",
                "quantity": 1,
                "price": 10,
                "direction": "buy",
                "offset": "OPEN",
                "position_side": "long",
                "source": "B",
                "source_seq": i + 1,
                "execution_id": "scale-E",
            },
        )
    if interleaved:
        # Reverse order puts updates far from their first observations without changing physical facts.
        for i in reversed(range(identities)):
            emit("/trade", {"identity_quality": "stable", "trade_key": f"T{i:06d}", "execution_id": "scale-E"})
    baseline = identities // 2
    for _ in range(proofs):
        positions = [
            {
                "symbol": symbol,
                "position_side": "long",
                "quantity": len(range(i, baseline, symbols)),
                "today_quantity": len(range(i, baseline, symbols)),
                "yesterday_quantity": 0,
                "available_quantity": len(range(i, baseline, symbols)),
            }
            for i, symbol in enumerate(names)
        ]
        emit(
            "/account_observation",
            {
                "scope": {"kind": "account"},
                "positions": positions,
                "position_basis": {
                    "status": "aligned",
                    "proof": {
                        "kind": "included_trade_keys",
                        "complete": True,
                        "included_trade_keys": [f"T{i:06d}" for i in range(baseline)],
                    },
                },
            },
        )
    if interleaved:
        emit(
            "/account_observation",
            {
                "scope": {"kind": "account"},
                "positions": [
                    {
                        "symbol": symbol,
                        "position_side": "long",
                        "today_quantity": len(range(i, identities, symbols)),
                        "yesterday_quantity": 0,
                        "position_basis": {
                            "status": "aligned",
                            "proof": {"kind": "source_watermark", "source": "B", "through": identities},
                        },
                    }
                    for i, symbol in enumerate(names)
                ],
            },
        )
    for symbol in names:
        emit(f"/quote/{symbol}", {"symbol": symbol, "book_valid": True, "bids": [[12, 1]], "asks": [[13, 1]]})
    writer.finish()
    return {
        "recording": recording,
        "identities": identities,
        "proofs": proofs,
        "symbols": names,
        "interleaved": interleaved,
        "sequence": sequence,
        "expected_quantity": identities,
        "expected_amount": identities * 10,
        "expected_vwap": 10,
    }


def main():
    """Write a separately named stress recording in the specified isolated root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--account", type=int, default=7)
    parser.add_argument("--recording", required=True)
    parser.add_argument("--identities", type=int, default=40_000)
    parser.add_argument("--proofs", type=int, default=16)
    parser.add_argument("--symbols", type=int, default=1)
    parser.add_argument("--interleaved", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            generate(
                args.root, args.account, args.recording, args.identities, args.proofs, args.interleaved, args.symbols
            )
        )
    )


if __name__ == "__main__":
    main()
