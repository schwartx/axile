"""Compact market identity and raw-reference queries; no payload decoding or aggregation."""

from __future__ import annotations

import json

from axile.server.execution_replay.raw_index import _stream_ids, integer
from axile.server.execution_replay.reader import BLOCK_COLUMNS, ReadError

SAFE = 9_007_199_254_740_991


def _subjects(db, rec, request):
    ids = request.get("objects")
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 128
        or len(set(integer(i, minimum=1) for i in ids)) != len(ids)
    ):
        raise ReadError("REPLAY_OBJECT_BUDGET", 422)
    marks = ",".join("?" for _ in ids)
    rows = list(
        db.execute(
            f"SELECT * FROM market_objects WHERE recording=? AND first_seq IN ({marks}) AND first_seq<=?",
            [rec, *ids, request["cutoff_seq"]],
        )
    )
    if len(rows) != len(ids):
        raise ReadError("REPLAY_OBJECT_INVALID")
    return rows


def locations(db, request):
    """Stable namespace-major cursors and bounded reference pages within a validated source view."""
    recording, cutoff = request["recording"], integer(request.get("cutoff_seq"), maximum=request["through_seq"])
    owner = db.execute("SELECT id FROM recording_keys WHERE recording=?", [recording]).fetchone()
    operation = request["operation"]
    if operation == "market-objects":
        if owner is None:
            return {"subjects": [], "next": None}
        kind = request.get("kind")
        if kind not in {"market_trade", "weak_market_trade"}:
            raise ReadError("REPLAY_OBJECT_KIND", 422)
        after = request.get("after", [0, 0])
        if not isinstance(after, list) or len(after) != 2:
            raise ReadError("REPLAY_CURSOR_INVALID", 422)
        ns, seq = [integer(v) for v in after]
        symbol = request.get("scope_symbol")
        if not isinstance(symbol, str) or not 1 <= len(symbol.encode()) <= 1024:
            raise ReadError("REPLAY_SCOPE_INVALID", 422)
        result, size = [], 0
        for _ in range(32):
            current = (
                db.execute(
                    "SELECT id,prefix FROM market_namespaces WHERE recording=? AND id=?", [owner[0], ns]
                ).fetchone()
                if ns and seq < SAFE
                else None
            )
            if current is None:
                current = db.execute(
                    "SELECT n.id,n.prefix FROM market_namespaces n WHERE n.recording=? AND n.id>? AND EXISTS(SELECT 1 FROM market_namespace_symbols s WHERE s.namespace=n.id AND s.symbol IN ('',?) AND s.first_seq<=?) ORDER BY n.id LIMIT 1",
                    [owner[0], ns, symbol, cutoff],
                ).fetchone()
                seq = 0
            if current is None:
                return {"subjects": result, "next": None}
            ns = current["id"]
            rows = db.execute(
                "SELECT first_seq,suffix FROM market_objects INDEXED BY market_page WHERE recording=? AND kind=? AND namespace=? AND first_seq>? AND first_seq<=? ORDER BY first_seq LIMIT ?",
                [owner[0], int(kind == "weak_market_trade"), ns, seq, cutoff, 129 - len(result)],
            )
            for row in rows:
                key = current["prefix"] + row["suffix"] + "]" if current["prefix"] else row["suffix"]
                subject = {"id": row["first_seq"], "kind": kind, "key": key, "first_seq": row["first_seq"]}
                amount = len(json.dumps(subject, ensure_ascii=False).encode())
                if len(result) == 128 or result and size + amount > 64 * 1024:
                    return {"subjects": result, "next": [ns, seq]}
                result.append(subject)
                size += amount
                seq = row["first_seq"]
            seq = SAFE
        return {"subjects": result, "next": [ns, seq]}
    if owner is None:
        raise ReadError("REPLAY_OBJECT_INVALID")
    rec = owner[0]
    subjects = _subjects(db, rec, request)
    ids = [s["first_seq"] for s in subjects]
    marks = ",".join("?" for _ in ids)
    start = integer(request.get("after_seq", 0), maximum=cutoff)
    if operation == "market-origin":
        changed = db.execute(
            f"SELECT 1 FROM market_links WHERE recording=? AND object IN ({marks}) AND known_seq>? AND known_seq<=? AND seq<=? LIMIT 1",
            [rec, *ids, start, cutoff, start],
        ).fetchone()
        return {"rebuild": changed is not None}
    if operation == "market-links":
        if not any(s["linked"] for s in subjects):
            return {"links": [], "next": None}
        after = request.get("after", [0, 0])
        if not isinstance(after, list) or len(after) != 2:
            raise ReadError("REPLAY_CURSOR_INVALID", 422)
        seq, identity = [integer(v) for v in after]
        rows = [
            dict(r)
            for r in db.execute(
                f"SELECT l.seq,l.object,EXISTS(SELECT 1 FROM market_links x WHERE x.recording=l.recording AND x.seq=l.seq AND x.object!=l.object AND x.known_seq<=?) AS conflict FROM market_links l WHERE l.recording=? AND l.object IN ({marks}) AND (l.seq,l.object)>(?,?) AND l.seq>? AND l.known_seq<=? ORDER BY l.seq,l.object LIMIT 129",
                [cutoff, rec, *ids, seq, identity, start, cutoff],
            )
        ]
        page = rows[:128]
        for row in page:
            row["conflict"] = bool(row["conflict"])
        return {"links": page, "next": [page[-1]["seq"], page[-1]["object"]] if len(rows) > 128 else None}
    if operation == "market-streams":
        streams = {s["stream"] for s in subjects}
        for subject in subjects:
            if subject["last_seq"] > subject["first_seq"]:
                streams.update(
                    _stream_ids(
                        db,
                        "SELECT stream,known_seq AS first_seq FROM market_refs INDEXED BY market_ref_known WHERE recording=? AND object=? AND stream>? ORDER BY stream,known_seq,block LIMIT 1",
                        [rec, subject["first_seq"]],
                        cutoff,
                    )
                )
            if len(streams) > 16:
                raise ReadError("REPLAY_DEPENDENCY_WORKING_SET", 413)
        if any(s["linked"] for s in subjects):
            streams.update(
                r[0]
                for r in db.execute(
                    f"SELECT DISTINCT w.stream FROM market_links l JOIN market_objects w ON w.recording=l.recording AND w.first_seq=l.seq AND w.kind=1 WHERE l.recording=? AND l.object IN ({marks}) AND l.known_seq<=? LIMIT 17",
                    [rec, *ids, cutoff],
                )
            )
        if len(streams) > 16:
            raise ReadError("REPLAY_DEPENDENCY_WORKING_SET", 413)
        streams = sorted(streams)
        initial = [
            {
                "stream": s,
                **_blocks(
                    db,
                    recording,
                    rec,
                    subjects,
                    {**request, "stream": s, "after_block": 0, "page_bytes": 1024 * 1024 // len(streams)},
                    max(1, 128 // len(streams)),
                ),
            }
            for s in streams
        ]
        return {"streams": streams, "initial": initial}
    if operation != "market-blocks":
        raise ReadError("REPLAY_OPERATION", 422)
    return _blocks(db, recording, rec, subjects, request, 128)


def _blocks(db, recording, rec, subjects, request, limit):
    stream = integer(request.get("stream"), maximum=65535)
    after = integer(request.get("after_block", 0))
    start, cutoff = integer(request.get("after_seq", 0)), request["cutoff_seq"]
    ids = [s["first_seq"] for s in subjects]
    marks = ",".join("?" for _ in ids)
    if start:
        first = db.execute(
            "SELECT id FROM blocks WHERE recording=? AND stream=? AND first_seq<=? ORDER BY first_seq DESC LIMIT 1",
            [recording, stream, start],
        ).fetchone()
        if first:
            after = max(after, first[0] - 1)
    blocks = {s["block"] for s in subjects if s["stream"] == stream and s["block"] > after}
    for subject in subjects:
        if subject["last_seq"] > subject["first_seq"]:
            blocks.update(
                r[0]
                for r in db.execute(
                    "SELECT block FROM market_refs WHERE recording=? AND object=? AND stream=? AND block>? AND seq>? AND known_seq<=? ORDER BY block LIMIT ?",
                    [rec, subject["first_seq"], stream, after, start, cutoff, limit + 1],
                )
            )
    if any(s["linked"] for s in subjects):
        blocks.update(
            r[0]
            for r in db.execute(
                f"SELECT DISTINCT w.block FROM market_links l JOIN market_objects w ON w.recording=l.recording AND w.first_seq=l.seq AND w.kind=1 WHERE l.recording=? AND l.object IN ({marks}) AND w.stream=? AND w.block>? AND l.seq>? AND l.known_seq<=? ORDER BY w.block LIMIT ?",
                [rec, *ids, stream, after, start, cutoff, limit + 1],
            )
        )
    selected = sorted(blocks)[:limit]
    output, size = [], 0
    budget = integer(request.get("page_bytes", 1024 * 1024), minimum=1, maximum=1024 * 1024)
    for identity in selected:
        row = db.execute(
            f"SELECT {BLOCK_COLUMNS} FROM blocks WHERE recording=? AND id=? AND last_seq>? AND first_seq<=?",
            [recording, identity, start, cutoff],
        ).fetchone()
        if row is None:
            continue
        if output and size + row["length"] > budget:
            break
        output.append(dict(row))
        size += row["length"]
    return {"blocks": output, "next": output[-1]["id"] if output and len(blocks) > len(output) else None}
