"""有界解码直接定位元信息；不读取原始业务载荷。"""

from __future__ import annotations

import hashlib

from axile.server.execution_replay.reader import ReadError

SAFE = 9_007_199_254_740_991
PAGE_ROWS = 256
BLOCK_ROWS = 1024
PAGE_BYTES = PAGE_ROWS * 15 + 16
BLOCK_BYTES = 26 + BLOCK_ROWS * 16


class _Ints:
    def __init__(self, data, maximum, work):
        if not isinstance(data, bytes) or not 16 < len(data) <= maximum:
            raise ReadError("REPLAY_INDEX_LOCATIONS")
        work(len(data), len(data))
        if hashlib.sha256(data[:-16]).digest()[:16] != data[-16:]:
            raise ReadError("REPLAY_INDEX_LOCATIONS")
        self.data, self.offset = data[:-16], 0

    def read(self, maximum=SAFE):
        value = 0
        for shift in range(0, 64, 7):
            if self.offset >= len(self.data):
                break
            byte = self.data[self.offset]
            self.offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                if value > maximum or shift and byte == 0:
                    break
                return value
        raise ReadError("REPLAY_INDEX_LOCATIONS")

    def end(self):
        if self.offset != len(self.data):
            raise ReadError("REPLAY_INDEX_LOCATIONS")


def _page(row, work):
    count = row["last_seq"] - row["first_seq"] + 1
    if (
        not 1 <= count <= PAGE_ROWS
        or not 1 <= row["first_seq"] <= row["last_seq"] <= SAFE
        or not 0 <= row["first_time"] <= row["last_time"] <= SAFE
    ):
        raise ReadError("REPLAY_INDEX_LOCATIONS")
    source = _Ints(row["positions"], PAGE_BYTES, work)
    work(count * 3)
    time, block = row["first_time"], 0
    result = []
    for index in range(count):
        delta = source.read()
        if index == 0 and delta != 0:
            raise ReadError("REPLAY_INDEX_LOCATIONS")
        time += delta
        encoded = source.read(2 * 0xFFFFFFFF)
        block += (encoded >> 1) ^ -(encoded & 1)
        line = source.read(BLOCK_ROWS - 1)
        if time > SAFE or not 1 <= block <= 0xFFFFFFFF:
            raise ReadError("REPLAY_INDEX_LOCATIONS")
        result.append({"seq": row["first_seq"] + index, "time": time, "block": block, "line": line})
        if index % 16 == 0:
            work(0)
    source.end()
    if time != row["last_time"]:
        raise ReadError("REPLAY_INDEX_LOCATIONS")
    return result


def _selected(db, recording, stream, cutoff, bound, evidence, work):
    block = db.one("quote_block_seed", [recording, stream, cutoff])
    if block is None:
        return None
    for row in evidence:
        if row["block"] == block["id"]:
            return row
    blob = db.one("block_positions", [recording, block["id"]])[0]
    source = _Ints(blob, BLOCK_BYTES, work)
    count, time = source.read(BLOCK_ROWS), source.read()
    if not count:
        raise ReadError("REPLAY_INDEX_LOCATIONS")
    work(count * 2)
    sequence = block["first_seq"]
    selected = None
    for line in range(count):
        delta, elapsed = source.read(), source.read()
        if line == 0 and (delta or elapsed) or line > 0 and not delta:
            raise ReadError("REPLAY_INDEX_LOCATIONS")
        sequence += delta
        time += elapsed
        if time > SAFE or sequence > SAFE:
            raise ReadError("REPLAY_INDEX_LOCATIONS")
        if (time, sequence) <= bound and sequence <= cutoff:
            selected = {"seq": sequence, "time": time, "block": block["id"], "line": line}
        if line % 16 == 0:
            work(0)
    source.end()
    if sequence != block["last_seq"] or selected is None:
        raise ReadError("REPLAY_INDEX_LOCATIONS")
    return selected


def locate(db, recording, through, target, tie, stream, work):
    """按冻结提交定位精确观察、最近 32 条证据与所选流的最后观察。"""
    if through == 0:
        return None, []
    last = db.one("location_end", [recording, through])
    if last is None or last["last_seq"] != through:
        raise ReadError("REPLAY_INDEX_LOCATIONS")
    bound = min((target, tie), (last["last_time"], through))
    page = db.one("location_time", [recording, *bound])
    if page is None:
        return None, []
    rows = _page(page, work)
    evidence = [row for row in reversed(rows) if (row["time"], row["seq"]) <= bound and row["seq"] <= through][:32]
    if not evidence:
        raise ReadError("REPLAY_INDEX_LOCATIONS")
    # Each preceding page contributes at least one row; at most 32 pages are examined.
    while len(evidence) < 32 and page["first_seq"] > 1:
        previous = db.one("location_before", [recording, page["first_seq"]])
        if (
            previous is None
            or previous["last_seq"] + 1 != page["first_seq"]
            or previous["last_time"] > page["first_time"]
        ):
            raise ReadError("REPLAY_INDEX_LOCATIONS")
        evidence.extend(list(reversed(_page(previous, work)))[: 32 - len(evidence)])
        page = previous
    cutoff = evidence[0]["seq"]
    return _selected(db, recording, stream, cutoff, bound, evidence, work), evidence


def at_sequence(db, recording, through, sequence, work):
    """Locate one adjacent observation without loading a global event list."""
    if not 1 <= sequence <= through:
        return None
    page = db.one("location_sequence", [recording, sequence])
    if page is None or page["last_seq"] < sequence:
        raise ReadError("REPLAY_INDEX_LOCATIONS")
    return _page(page, work)[sequence - page["first_seq"]]
