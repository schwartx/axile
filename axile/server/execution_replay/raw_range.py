"""Bounded quote ranges and control prefixes for browser interval integration."""

from axile.server.execution_replay.raw_index import _stream_ids, integer
from axile.server.execution_replay.reader import ReadError


def locations(db, request, work):
    """Return the quote seed and indexed range without materializing its observations."""
    recording, cutoff = request["recording"], integer(request.get("cutoff_seq"), maximum=request["through_seq"])
    if request["operation"] == "business-blocks":
        after = request.get("after", [0, 0])
        if not isinstance(after, list) or len(after) != 2:
            raise ReadError("REPLAY_PLAN_CURSOR", 422)
        stream = integer(after[0], maximum=65535)
        sequence = integer(after[1], maximum=cutoff)
        sources = [r[0] for r in db.rows("business_streams", [recording, stream])]
        rows = []
        for source in sources:
            rows.extend(
                dict(r)
                for r in db.rows(
                    "business_blocks", [recording, source, sequence if source == stream else 0, cutoff, 129 - len(rows)]
                )
            )
            if len(rows) == 129:
                break
        page = rows[:128]
        return {"blocks": page, "next": [page[-1]["stream"], page[-1]["first_seq"]] if len(rows) > 128 else None}
    symbol = request.get("scope_symbol")
    if not isinstance(symbol, str) or not 1 <= len(symbol.encode()) <= 1024:
        raise ReadError("REPLAY_SCOPE_INVALID", 422)
    if request.get("catalog") is True:
        from axile.server.execution_replay.raw_locations import locate

        end = integer(request.get("interval_end_us"))
        _, evidence = locate(
            db, recording, request["through_seq"], end, 0, integer(request["stream"], maximum=65535), work
        )
        cutoff = min(cutoff, evidence[0]["seq"] if evidence else 0)
        request = {**request, "cutoff_seq": cutoff}
        streams = (
            set()
            if request.get("controls_only") is True
            else {r[0] for r in db.rows("topic_streams", [recording, "/quote/" + symbol])}
        )
        for topic in ["/connection", "/capture"]:
            for scope in ["", symbol]:
                streams.update(
                    _stream_ids(
                        db,
                        "next_scope_stream",
                        [recording, topic, scope],
                        cutoff,
                    )
                )
        if len(streams) > 16:
            raise ReadError("REPLAY_DEPENDENCY_WORKING_SET", 413)
        streams = sorted(streams)
        initial = [
            {
                "stream": stream,
                **_page(
                    db,
                    {**request, "stream": stream, "after_block": 0, "page_bytes": 1024 * 1024 // len(streams)},
                    max(1, 128 // len(streams)),
                    work,
                ),
            }
            for stream in streams
        ]
        return {"streams": streams, "initial": initial, "range_through": cutoff}
    if "range_through" in request:
        request = {**request, "cutoff_seq": integer(request["range_through"], maximum=cutoff)}
    return _page(db, request, 128, work)


def _page(db, request, limit, work):
    recording, cutoff, symbol = request["recording"], request["cutoff_seq"], request["scope_symbol"]
    stream = integer(request["stream"], maximum=65535)
    after = integer(request.get("after_block", 0))
    source = db.one("stream_topic", [recording, stream])
    if source is None:
        raise ReadError("REPLAY_STREAM_INVALID", 422)
    ids = set()
    if source[0] == "/quote/" + symbol:
        from axile.server.execution_replay.raw_locations import locate

        start = integer(request.get("interval_start_us"))
        selected, _ = locate(db, recording, request["through_seq"], start, 0, stream, work)
        low = selected["seq"] if selected else 0
        if selected:
            low = db.one("block_start", [recording, selected["block"]])[0]
        if after:
            previous = db.one("stream_block_start", [recording, after, stream])
            if previous is None:
                raise ReadError("REPLAY_PLAN_CURSOR")
            low = max(low, previous[0] + 1)
        ids.update(r[0] for r in db.rows("quote_blocks", [recording, stream, low, cutoff, limit + 1]))
    elif source[0] in {"/connection", "/capture"}:
        for scope in ["", symbol]:
            ids.update(
                r[0] for r in db.rows("control_blocks", [recording, source[0], scope, stream, after, cutoff, limit + 1])
            )
    else:
        raise ReadError("REPLAY_STREAM_INVALID", 422)
    result, size = [], 0
    budget = integer(request.get("page_bytes", 1024 * 1024), minimum=1, maximum=1024 * 1024)
    for identity in sorted(ids)[:limit]:
        row = db.one("block", [recording, identity])
        if result and size + row["length"] > budget:
            break
        result.append(dict(row))
        size += row["length"]
    return {"blocks": result, "next": result[-1]["id"] if result and len(ids) > len(result) else None}
