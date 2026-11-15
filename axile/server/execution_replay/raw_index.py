"""有界的原始身份、块位置和证明成员查询；不读取或解释业务载荷。"""

from __future__ import annotations

import json

from axile.server.execution_replay.reader import BLOCK_COLUMNS, ReadError

PAGE = 8
DEPENDENCY_PAGE = 128
BLOCK_PAGE = 128
KINDS = {"trade", "weak_trade", "command", "order", "position", "execution"}


def integer(value, *, maximum=9_007_199_254_740_991, minimum=0):
    """拒绝布尔值、大整数和跨边界游标。"""
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReadError("REPLAY_CURSOR_INVALID", 422)
    return value


def _decorate(db, recording, rows):
    for row in rows:
        if row["kind"] == "order":
            symbol = json.loads(row["key"])[0]
            first = db.execute(
                "SELECT first_seq FROM symbols WHERE recording=? AND symbol=?", [recording, symbol]
            ).fetchone()
            row["aux"] = {"symbol_first_seq": first[0] if first else None}
    return rows


def _subjects(db, request):
    ids = request.get("objects")
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= DEPENDENCY_PAGE
        or len({integer(i, minimum=1) for i in ids}) != len(ids)
    ):
        raise ReadError("REPLAY_OBJECT_BUDGET", 422)
    # Otherwise SQLite can scan objects_page for every fixed batch.
    placeholders = ",".join("?" for _ in ids)
    rows = {
        row["id"]: dict(row)
        for row in db.execute(
            f"SELECT id,kind,key,first_seq FROM objects NOT INDEXED WHERE recording=? AND id IN ({placeholders}) AND first_seq<=?",
            [request["recording"], *ids, request["through_seq"]],
        )
    }
    if len(rows) != len(ids):
        raise ReadError("REPLAY_OBJECT_INVALID")
    return [rows[identity] for identity in ids]


def _scopes(subjects):
    scopes = set()
    for subject in subjects:
        if subject["kind"] in {"order", "position"}:
            symbol = json.loads(subject["key"])[0]
            topics = (
                ("/order_set", "/connection")
                if subject["kind"] == "order"
                else ("/account_observation", "/connection", "/capture")
            )
            for topic in topics:
                scopes.add((topic, ""))
                scopes.add((topic, symbol))
    return sorted(scopes)


def _stream_ids(db, statement, values, cutoff):
    # Seek past the entire previous stream, including all of its future blocks.
    after = -1
    result = []
    for _ in range(129):
        row = db.execute(statement, [*values, after]).fetchone()
        if row is None:
            break
        after = row["stream"]
        if row["first_seq"] <= cutoff:
            result.append(after)
    return result


def locations(db, request):
    """每页最多 8 个身份或 128 个块；游标只在已验证的源代次内使用。"""
    recording, operation = request["recording"], request["operation"]
    cutoff = integer(request.get("cutoff_seq"), maximum=request["through_seq"])
    if operation == "dependency-boundary":
        subjects = _subjects(db, request)
        after = integer(request.get("after_seq", 0), maximum=cutoff)
        changed = any(
            db.execute(
                "SELECT 1 FROM relations r JOIN objects o ON o.id=r.target WHERE r.recording=? AND r.source=? AND r.role='related_capture_seq' AND r.seq>? AND r.seq<=? AND o.first_seq<=? LIMIT 1",
                [recording, s["id"], after, cutoff, after],
            ).fetchone()
            for s in subjects
        )
        return {"rebuild": changed}
    if operation == "object-links":
        subjects = _subjects(db, request)
        after = request.get("after", [0, 0])
        if not isinstance(after, list) or len(after) != 2:
            raise ReadError("REPLAY_CURSOR_INVALID", 422)
        seq, identity = [integer(v) for v in after]
        start = integer(request.get("after_seq", 0), maximum=cutoff)
        ids = [s["id"] for s in subjects]
        marks = ",".join("?" for _ in ids)
        rows = [
            dict(row)
            for row in db.execute(
                f"SELECT o.first_seq AS seq,r.source AS object,EXISTS(SELECT 1 FROM relations x WHERE x.recording=r.recording AND x.target=r.target AND x.role=r.role AND x.source<>r.source AND x.seq<=?) AS conflict FROM relations r JOIN objects o ON o.id=r.target WHERE r.recording=? AND r.source IN ({marks}) AND r.role='related_capture_seq' AND r.seq<=? AND o.first_seq>? AND (o.first_seq,r.source)>(?,?) GROUP BY o.first_seq,r.source ORDER BY o.first_seq,r.source LIMIT 129",
                [cutoff, recording, *ids, cutoff, start, seq, identity],
            )
        ]
        page = rows[:128]
        for row in page:
            row["conflict"] = bool(row["conflict"])
        return {"links": page, "next": [page[-1]["seq"], page[-1]["object"]] if len(rows) > 128 else None}
    if operation == "versions":
        after = integer(request.get("after", 0))
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT seq,generation FROM generations WHERE recording=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                [recording, after, cutoff, BLOCK_PAGE + 1],
            )
        ]
        page = rows[:BLOCK_PAGE]
        return {"versions": page, "next": page[-1]["seq"] if len(rows) > BLOCK_PAGE else None}
    if operation == "commands":
        limit = integer(request.get("limit", PAGE), minimum=1, maximum=DEPENDENCY_PAGE)
        after = request.get("after", [0, 0])
        if not isinstance(after, list) or len(after) != 2:
            raise ReadError("REPLAY_CURSOR_INVALID", 422)
        after_seq, after_id = [integer(v) for v in after]
        keys = request.get("order_ids")
        if "objects" in request:
            keys = set()
            for subject in _subjects(db, request):
                if subject["kind"] == "order":
                    keys.add(json.loads(subject["key"])[1])
                elif subject["kind"] in {"trade", "weak_trade"}:
                    mentions = list(
                        db.execute(
                            "SELECT order_id FROM order_mentions WHERE recording=? AND object=? UNION SELECT m.order_id FROM relations r JOIN order_mentions m ON m.recording=r.recording AND m.object=r.target WHERE r.recording=? AND r.source=? AND r.role='related_capture_seq' AND r.seq<=? ORDER BY order_id LIMIT ?",
                            [recording, subject["id"], recording, subject["id"], cutoff, PAGE + 1],
                        )
                    )
                    if len(mentions) > PAGE:
                        raise ReadError("REPLAY_IDENTITY_CONFLICT")
                    keys.update(row[0] for row in mentions)
                else:
                    raise ReadError("REPLAY_OBJECT_KIND", 422)
        elif (
            not isinstance(keys, list)
            or not 1 <= len(keys) <= PAGE
            or any(not isinstance(k, str) or not 1 <= len(k) <= 4096 for k in keys)
        ):
            raise ReadError("REPLAY_OBJECT_BUDGET", 422)
        candidates = set()
        for key in keys:
            candidates.update(
                tuple(row)
                for row in db.execute(
                    "SELECT first_seq,object FROM order_mentions WHERE recording=? AND kind='command' AND order_id=? AND (first_seq,object)>(?,?) AND first_seq<=? ORDER BY first_seq,object LIMIT ?",
                    [recording, key, after_seq, after_id, cutoff, limit + 1],
                )
            )
        candidates = sorted(candidates)
        # One command can mention several raw order IDs; advance over the scanned tuples.
        selected = list(dict.fromkeys(row[1] for row in candidates[:limit]))
        rows = _subjects(db, {**request, "objects": selected}) if selected else []
        page, size = [], 0
        for row in rows:
            amount = len(json.dumps(row, ensure_ascii=False).encode())
            if page and size + amount > 64 * 1024:
                break
            size += amount
            page.append(row)
        # Advance only past tuples whose identities were included, preserving one-to-many mentions.
        included = {r["id"] for r in page}
        consumed = []
        for candidate in candidates[:limit]:
            if candidate[1] not in included:
                break
            consumed.append(candidate)
        return {"subjects": page, "next": list(consumed[-1]) if len(candidates) > len(consumed) else None}
    if operation == "objects":
        kind = request.get("kind")
        limit = integer(request.get("limit", PAGE), minimum=1, maximum=DEPENDENCY_PAGE)
        if limit > PAGE and kind not in {"trade", "weak_trade"}:
            raise ReadError("REPLAY_OBJECT_BUDGET", 422)
        if kind not in KINDS:
            raise ReadError("REPLAY_OBJECT_KIND", 422)
        cursor = request.get("after", [0, 0])
        if not isinstance(cursor, list) or len(cursor) != 2:
            raise ReadError("REPLAY_CURSOR_INVALID", 422)
        seq, identity = [integer(v) for v in cursor]
        if request.get("planning") is True and kind in {"trade", "weak_trade"}:
            rows = [
                tuple(r)
                for r in db.execute(
                    "SELECT first_seq,id FROM objects WHERE recording=? AND kind=? AND (first_seq,id)>(?,?) AND first_seq<=? ORDER BY first_seq,id LIMIT ?",
                    [recording, kind, seq, identity, cutoff, limit + 1],
                )
            ]
            return {"ids": rows[:limit], "next": rows[limit - 1] if len(rows) > limit else None}
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT id,kind,key,first_seq FROM objects WHERE recording=? AND kind=? AND (first_seq,id)>(?,?) AND first_seq<=? ORDER BY first_seq,id LIMIT ?",
                [recording, kind, seq, identity, cutoff, limit + 1],
            )
        ]
        page, size = [], 0
        for row in rows[:limit]:
            size += len(json.dumps(row, ensure_ascii=False).encode())
            if page and size > 64 * 1024:
                break
            page.append(row)
        page = _decorate(db, recording, page)
        if limit > PAGE and kind in {"trade", "weak_trade"}:
            return {
                "ids": [[r["first_seq"], r["id"]] for r in page],
                "keys": [r["key"] for r in page],
                "next": [page[-1]["first_seq"], page[-1]["id"]] if len(rows) > len(page) else None,
            }
        return {"subjects": page, "next": [page[-1]["first_seq"], page[-1]["id"]] if len(rows) > len(page) else None}
    if operation == "members":
        seq, row = integer(request.get("proof_seq"), minimum=1, maximum=cutoff), request.get("row")
        if type(row) is not int or not -1 <= row <= 16_384:
            raise ReadError("REPLAY_PROOF_INVALID", 422)
        proof = db.execute(
            "SELECT members FROM proofs WHERE recording=? AND seq=? AND row=?", [recording, seq, row]
        ).fetchone()
        if proof is None:
            # Absence of the proof itself never establishes member absence.
            raise ReadError("REPLAY_PROOF_UNPUBLISHED")
        keys = request.get("keys")
        if "objects" in request:
            subjects = _subjects(db, request)
            members = []
            for subject in subjects:
                member = (
                    db.execute(
                        "SELECT position FROM proof_members WHERE recording=? AND seq=? AND row=? AND member=?",
                        [recording, seq, row, subject["id"]],
                    ).fetchone()
                    if subject["kind"] == "trade"
                    else None
                )
                members.append(member[0] if member else None)
            return {"proof_seq": seq, "row": row, "members_count": proof[0], "positions": members}
        if (
            not isinstance(keys, list)
            or len(keys) > DEPENDENCY_PAGE
            or any(k is not None and (not isinstance(k, str) or not 1 <= len(k) <= 4096) for k in keys)
        ):
            raise ReadError("REPLAY_PROOF_BUDGET", 422)
        members = []
        for key in keys:
            member = db.execute(
                "SELECT m.position FROM objects o JOIN proof_members m ON m.member=o.id AND m.recording=o.recording WHERE o.recording=? AND o.kind='trade' AND o.key=? AND m.seq=? AND m.row=?",
                [recording, key, seq, row],
            ).fetchone()
            members.append(member[0] if member else None)
        return {"proof_seq": seq, "row": row, "members_count": proof[0], "positions": members}
    subjects = _subjects(db, request)
    scopes = _scopes(subjects)
    orders_only = all(subject["kind"] == "order" for subject in subjects)
    if operation == "queries":
        after = integer(request.get("after", 0))
        sequences = set()
        for subject in subjects:
            sequences.update(
                row[0]
                for row in db.execute(
                    "SELECT r.seq FROM object_refs r JOIN query_refs q ON q.recording=r.recording AND q.seq=r.seq WHERE r.recording=? AND r.object=? AND r.seq>? AND r.seq<=? ORDER BY r.seq LIMIT ?",
                    [recording, subject["id"], after, cutoff, BLOCK_PAGE + 1],
                )
            )
        for topic, symbol in scopes:
            sequences.update(
                row[0]
                for row in db.execute(
                    "SELECT r.seq FROM scope_refs r JOIN query_refs q ON q.recording=r.recording AND q.seq=r.seq WHERE r.recording=? AND r.kind=? AND r.symbol=? AND r.seq>? AND r.seq<=? ORDER BY r.seq LIMIT ?",
                    [recording, topic, symbol, after, cutoff, BLOCK_PAGE + 1],
                )
            )
        page = sorted(sequences)[:BLOCK_PAGE]
        return {"sequences": page, "next": page[-1] if len(sequences) > BLOCK_PAGE else None}
    if operation == "object-streams":
        streams = set()
        stream_subjects = {}
        stream_scopes = {}
        if len(subjects) > PAGE:
            ids = [s["id"] for s in subjects if s["kind"] != "position"]
            marks = ",".join("?" for _ in ids)
            streams = {
                r[0]
                for r in db.execute(
                    f"SELECT DISTINCT stream FROM object_blocks WHERE recording=? AND object IN ({marks}) AND first_seq<=? LIMIT 17",
                    [recording, *ids, cutoff],
                )
            }
            stream_subjects = {s: subjects for s in streams}
        for subject in subjects if len(subjects) <= PAGE else []:
            if subject["kind"] == "position":
                continue
            for stream in _stream_ids(
                db,
                "SELECT stream,first_seq FROM object_blocks WHERE recording=? AND object=? AND stream>? ORDER BY stream,block LIMIT 1",
                [recording, subject["id"]],
                cutoff,
            ):
                streams.add(stream)
                stream_subjects.setdefault(stream, []).append(subject)
        for topic, symbol in scopes:
            for stream in _stream_ids(
                db,
                "SELECT stream,first_seq FROM scope_blocks WHERE recording=? AND kind=? AND symbol=? AND stream>? ORDER BY stream,block LIMIT 1",
                [recording, topic, symbol],
                cutoff,
            ):
                streams.add(stream)
                stream_scopes.setdefault(stream, []).append((topic, symbol))
        if orders_only and streams:
            placeholders = ",".join("?" for _ in streams)
            streams = {
                row[0]
                for row in db.execute(
                    f"SELECT id FROM streams WHERE recording=? AND id IN ({placeholders}) AND topic IN ('/order','/order_set','/connection')",
                    [recording, *sorted(streams)],
                )
            }
        if len(streams) > 16:
            raise ReadError("REPLAY_DEPENDENCY_WORKING_SET", 413)
        streams = sorted(streams)
        initial = [
            {
                "stream": stream,
                **_blocks(
                    db,
                    {**request, "stream": stream, "after_block": 0, "page_bytes": 1024 * 1024 // len(streams)},
                    stream_subjects.get(stream, []),
                    stream_scopes.get(stream, []),
                    max(1, BLOCK_PAGE // len(streams)),
                ),
            }
            for stream in streams
        ]
        return {"streams": streams, "initial": initial}
    if operation != "object-blocks":
        raise ReadError("REPLAY_OPERATION", 422)
    return _blocks(db, request, subjects, scopes, BLOCK_PAGE)


def _blocks(db, request, subjects, scopes, page_size):
    recording = request["recording"]
    cutoff = request["cutoff_seq"]
    orders_only = all(subject["kind"] == "order" for subject in subjects)
    stream = integer(request.get("stream"), maximum=65535)
    after = integer(request.get("after_block", 0))
    start = integer(request.get("after_seq", 0), maximum=cutoff)
    last = db.execute(
        "SELECT id FROM blocks WHERE recording=? AND stream=? AND first_seq<=? ORDER BY first_seq DESC LIMIT 1",
        [recording, stream, cutoff],
    ).fetchone()
    if last is None:
        return {"blocks": [], "next": None}
    first = (
        db.execute(
            "SELECT id FROM blocks WHERE recording=? AND stream=? AND first_seq<=? ORDER BY first_seq DESC LIMIT 1",
            [recording, stream, start],
        ).fetchone()
        if start
        else None
    )
    after = max(after, first[0] - 1 if first else 0)
    ids = set()
    topic = db.execute("SELECT topic FROM streams WHERE recording=? AND id=?", [recording, stream]).fetchone()
    if len(subjects) > PAGE:
        subjects_ids = [s["id"] for s in subjects if s["kind"] != "position"]
        marks = ",".join("?" for _ in subjects_ids)
        ids.update(
            r[0]
            for r in db.execute(
                f"SELECT DISTINCT block FROM object_blocks WHERE recording=? AND object IN ({marks}) AND stream=? AND block>? AND block<=? AND first_seq<=? AND last_seq>? ORDER BY block LIMIT ?",
                [recording, *subjects_ids, stream, after, last[0], cutoff, start, page_size + 1],
            )
        )
    for subject in subjects if len(subjects) <= PAGE else []:
        if subject["kind"] == "position" or orders_only and (topic is None or topic[0] not in {"/order", "/order_set"}):
            continue
        ids.update(
            row[0]
            for row in db.execute(
                "SELECT block FROM object_blocks WHERE recording=? AND object=? AND stream=? AND block>? AND block<=? AND first_seq<=? AND last_seq>? ORDER BY block LIMIT ?",
                [recording, subject["id"], stream, after, last[0], cutoff, start, page_size + 1],
            )
        )
    for topic, symbol in scopes:
        ids.update(
            row[0]
            for row in db.execute(
                "SELECT block FROM scope_blocks WHERE recording=? AND kind=? AND symbol=? AND stream=? AND block>? AND block<=? AND first_seq<=? AND last_seq>? ORDER BY block LIMIT ?",
                [recording, topic, symbol, stream, after, last[0], cutoff, start, page_size + 1],
            )
        )
    selected = sorted(ids)[:page_size]
    budget = integer(request.get("page_bytes", 1024 * 1024), minimum=1, maximum=1024 * 1024)
    blocks = []
    size = 0
    if selected:
        placeholders = ",".join("?" for _ in selected)
        for row in db.execute(
            f"SELECT {BLOCK_COLUMNS} FROM blocks WHERE recording=? AND id IN ({placeholders}) ORDER BY id",
            [recording, *selected],
        ):
            if blocks and size + row["length"] > budget:
                break
            blocks.append(dict(row))
            size += row["length"]
    return {"blocks": blocks, "next": blocks[-1]["id"] if len(ids) > len(blocks) else None}
