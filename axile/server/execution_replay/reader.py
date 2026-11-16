"""Bounded raw-v2 reads and server-owned admission fences; no replay projection."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

WINDOW = 10.0
WINDOW_BYTES = 32 * 1024 * 1024
ROOT_BYTES = 8 * 1024 * 1024
LEASE = 90.0
ROOT_SECONDS = 60.0
MAX_BLOCK = 600 * 1024
MAX_BATCH = 1024 * 1024
MAX_PACKED = MAX_BATCH + 8 + 128 * 8
MAX_RAW = 512 * 1024 + 1
METADATA_CREDIT = 256 * 1024
WINDOW_INDEX_STEPS = 2_000_000
REQUEST_INDEX_STEPS = 200_000
# A 40,000-identity fold spans many bounded reads; the account rate and root deadline still apply.
ROOT_INDEX_STEPS = 8_000_000
INTERVAL_BYTES = 128 * 1024 * 1024
INTERVAL_INDEX_STEPS = 16_000_000
INTERVAL_SECONDS = 120.0


class ReadError(Exception):
    """可返回客户端的读取拒绝原因。"""

    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass
class Root:
    """单个有限根任务的 fence 和累计成本。"""

    sequence: int
    started: float
    attempt: int = 0
    canceled_through: int = 0
    sealed: bool = False
    active: int = 0
    bytes: int = 0
    index_steps: int = 0
    index_bytes: int = 0
    raw_bytes: int = 0
    reserved_slot: int = 0
    reserved_steps: int = 0
    profile: str | None = None
    byte_limit: int = ROOT_BYTES
    step_limit: int = ROOT_INDEX_STEPS
    seconds: float = ROOT_SECONDS
    pins: dict = field(default_factory=dict)


@dataclass
class ReadSession:
    """有期限的读会话；已回收根序号不得复用。"""

    account: int
    execution: str
    expires: float
    ack: int = 0
    root: Root | None = None
    total_bytes: int = 0
    total_index_steps: int = 0
    total_index_bytes: int = 0
    raw_bytes: int = 0


@dataclass
@dataclass(kw_only=True)
class Account:
    """跨会话保留的有界速率桶和累计标量。"""

    buckets: dict[int, int] = field(default_factory=dict)
    step_buckets: dict[int, int] = field(default_factory=dict)
    control_buckets: dict[int, int] = field(default_factory=dict)
    total_controls: int = 0
    active: int = 0
    total_bytes: int = 0
    total_index_steps: int = 0
    total_index_bytes: int = 0
    raw_bytes: int = 0


class Readers:
    """One owner process; epoch tokens never migrate live jobs to another owner."""

    def __init__(self, now=time.monotonic):
        self.now = now
        self.epoch = uuid4().hex
        self.lock = threading.RLock()
        self.sessions: dict[str, ReadSession] = {}
        # shortcut: 速率桶仅保存在进程内；需要跨重启限流时再持久化。
        self.accounts: dict[int, Account] = {}
        self._owner = None
        self._owner_root: str | None = None
        self._secret = os.urandom(32)
        self.recovered: dict[tuple, float] = {}
        self.maintaining = False

    @contextlib.contextmanager
    def maintenance(self):
        """Retire live reads before an explicit import replaces the local index generation."""
        with self.lock:
            if self.maintaining:
                raise ReadError("REPLAY_READ_WAIT", 429)
            self.maintaining = True
            for session in self.sessions.values():
                if session.root:
                    session.root.sealed = True
        try:
            until = time.monotonic() + 4
            while True:
                with self.lock:
                    stopped = not any(a.active for a in self.accounts.values())
                if stopped:
                    break
                if time.monotonic() >= until:
                    raise ReadError("REPLAY_RECOVERY_STOPPING")
                time.sleep(0.01)
            with self.lock:
                self.sessions.clear()
                self._owner = None
                self.epoch = uuid4().hex
            yield
        finally:
            with self.lock:
                self.maintaining = False

    def proof(self, account, execution, epoch, sid):
        """Bind a stopping proof to its original account, execution and transport."""
        return hmac.new(
            self._secret, json.dumps([account, execution, epoch, sid], separators=(",", ":")).encode(), hashlib.sha256
        ).hexdigest()

    def old(self, account, execution, request):
        """Reject an unknown UUID as evidence that an old read actually stopped."""
        proof = request.get("session_proof")
        if not isinstance(proof, str) or not hmac.compare_digest(
            proof, self.proof(account, execution, request.get("reader_epoch"), request.get("session"))
        ):
            raise ReadError("REPLAY_STOP_PROOF")

    def open(self, account: int, execution: str, root_path: str | Path | None = None, resume=None) -> dict:
        """创建绑定实际读取实例的短期会话，不清零账户计量。"""
        with self.lock:
            if self.maintaining:
                raise ReadError("REPLAY_READ_WAIT", 429)
            if root_path is not None:
                self._claim_owner(root_path)
            self._expire()
            if len(self.sessions) >= 64 or sum(s.account == account for s in self.sessions.values()) >= 4:
                raise ReadError("REPLAY_READER_CAPACITY", 429)
            if account not in self.accounts and len(self.accounts) >= 64:
                raise ReadError("REPLAY_READER_CAPACITY", 429)
            sid = uuid4().hex
            self.sessions[sid] = ReadSession(account, execution, self.now() + LEASE)
            self.accounts.setdefault(account, Account())
            if resume is not None:
                try:
                    self.old(account, execution, resume)
                    previous = None
                    if resume.get("reader_epoch") == self.epoch:
                        previous = self.sessions.get(resume.get("session"))
                        if previous and previous.root and previous.root.active:
                            raise ReadError("REPLAY_RECOVERY_STOPPING", 429)
                        if previous and (
                            previous.root is None
                            or previous.root.sealed
                            or previous.root.sequence != resume.get("root_seq")
                        ):
                            raise ReadError("REPLAY_RECOVERY_RETIRED")
                    self.recovered = {key: until for key, until in self.recovered.items() if until > self.now()}
                    key = (resume.get("reader_epoch"), resume.get("session"), resume.get("root_seq"))
                    if key in self.recovered or len(self.recovered) >= 64:
                        raise ReadError("REPLAY_RECOVERY_RETIRED")
                    attempt = resume.get("attempt")
                    elapsed = resume.get("elapsed_ms")
                    spent = resume.get("spent_bytes")
                    steps = resume.get("spent_steps")
                    total = resume.get("total_bytes")
                    root = Root(1, self.now())
                    self.configure(root, resume)
                    if (
                        type(attempt) is not int
                        or not 2 <= attempt <= 3
                        or type(elapsed) not in (int, float)
                        or not 0 <= elapsed < root.seconds * 1000
                        or type(spent) is not int
                        or not 0 <= spent <= root.byte_limit
                        or type(steps) is not int
                        or not 0 <= steps <= root.step_limit
                        or type(total) is not int
                        or not spent <= total <= 9_007_199_254_740_991
                    ):
                        raise ReadError("REPLAY_RECOVERY_BUDGET", 422)
                    if previous:
                        prior = previous.root
                        if attempt != prior.attempt + 1 or root.profile != prior.profile:
                            raise ReadError("REPLAY_RECOVERY_BUDGET", 422)
                        # A delayed client watermark cannot erase work still known by this owner.
                        spent, steps = max(spent, prior.bytes), max(steps, prior.index_steps)
                        elapsed = max(elapsed, (self.now() - prior.started) * 1000)
                        total = max(total, previous.total_bytes)
                        root.raw_bytes, root.index_bytes = prior.raw_bytes, prior.index_bytes
                        self.sessions[sid].raw_bytes = previous.raw_bytes
                        self.sessions[sid].total_index_bytes = previous.total_index_bytes
                        self.sessions[sid].total_index_steps = previous.total_index_steps
                        prior.sealed = True
                        prior.pins.clear()
                        del self.sessions[resume["session"]]
                    else:
                        self.sessions[sid].total_index_steps = steps
                    root.started -= elapsed / 1000
                    root.bytes, root.index_steps = spent, steps
                    root.attempt = root.canceled_through = attempt - 1
                    self.sessions[sid].root = root
                    self.sessions[sid].total_bytes = total
                    self.recovered[key] = self.now() + root.seconds
                except Exception:
                    del self.sessions[sid]
                    raise
            return {
                "reader_epoch": self.epoch,
                "session": sid,
                "session_proof": self.proof(account, execution, self.epoch, sid),
                "lease_seconds": LEASE,
            }

    def _claim_owner(self, root_path: str | Path) -> None:
        from axile.server._native import _core

        path = str(Path(root_path).resolve())
        if self._owner is not None:
            if path != self._owner_root:
                raise ReadError("REPLAY_OWNER_ROOT_CHANGED")
            return
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            self._owner = _core.ReplayReadOwner(path)
        except (OSError, _core.NativeError) as exc:
            raise ReadError("REPLAY_READ_OWNER_UNAVAILABLE", 503) from exc
        self._owner_root = path
        secret_path = Path(path) / ".read-proof-v2"
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            fd = os.open(secret_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                self._secret = os.read(fd, 33)
                if len(self._secret) != 32:
                    raise ReadError("REPLAY_STOP_PROOF")
            finally:
                os.close(fd)
        else:
            try:
                os.write(fd, self._secret)
                os.fsync(fd)
            finally:
                os.close(fd)

    def _expire(self) -> None:
        for sid, session in list(self.sessions.items()):
            if self.now() >= session.expires:
                if session.root:
                    session.root.sealed = True
                if session.root is None or not session.root.active:
                    del self.sessions[sid]

    def session(self, account: int, execution: str, request: dict, *, control=False) -> ReadSession:
        """验证读取实例、会话归属及数据通道租约。"""
        if request.get("reader_epoch") != self.epoch:
            # The old process may still be alive. Never claim its jobs retired from a new epoch.
            raise ReadError("REPLAY_READER_EPOCH")
        session = self.sessions.get(request.get("session"))
        if session is None or (session.account, session.execution) != (account, execution):
            raise ReadError("REPLAY_READER_SESSION")
        if not control and self.now() >= session.expires:
            raise ReadError("REPLAY_READER_EXPIRED")
        return session

    def root(self, session: ReadSession, sequence: int) -> Root:
        """只准入 ACK 水位之后紧邻的一个根任务。"""
        if type(sequence) is not int or sequence <= session.ack or sequence != session.ack + 1:
            raise ReadError("REPLAY_ROOT_RETIRED")
        if session.root is None:
            session.root = Root(sequence, self.now())
        return session.root

    def configure(self, root: Root, request: dict):
        """Freeze an operation envelope before its first read; account rates remain independent."""
        profile = request.get("profile", "point")
        if profile not in {"point", "interval"} or root.profile is not None and root.profile != profile:
            raise ReadError("REPLAY_ROOT_PROFILE", 422)
        if root.profile is None:
            root.profile = profile
            if profile == "interval":
                root.byte_limit, root.step_limit, root.seconds = INTERVAL_BYTES, INTERVAL_INDEX_STEPS, INTERVAL_SECONDS

    def control(self, account: int, execution: str, request: dict) -> dict:
        """控制通道不重做读取，保留取消和回收后的拒绝水位。"""
        with self.lock:
            if request.get("reader_epoch") != self.epoch:
                self.old(account, execution, request)
                if self._owner is None:
                    raise ReadError("REPLAY_STOP_PROOF")
                return {
                    "state": "retired",
                    "estimated": True,
                    "old_owner_stopped": True,
                }
            owner = self.accounts.get(account)
            if owner is not None:
                owner.control_buckets = {s: n for s, n in owner.control_buckets.items() if self.now() - s < 2}
                if sum(owner.control_buckets.values()) >= 256:
                    raise ReadError("REPLAY_CONTROL_RATE", 429)
                slot = int(self.now())
                owner.control_buckets[slot] = owner.control_buckets.get(slot, 0) + 1
                owner.total_controls += 1
            if request.get("reader_epoch") == self.epoch and request.get("session") not in self.sessions:
                self.old(account, execution, request)
                return {"state": "retired", "estimated": True}
            session = self.session(account, execution, request, control=True)
            seq, operation = request.get("root_seq"), request.get("operation")
            if operation == "renew":
                if self.now() >= session.expires:
                    raise ReadError("REPLAY_READER_EXPIRED")
                session.expires = self.now() + LEASE
                return {"renewed": True}
            if type(seq) is not int or seq < 1:
                raise ReadError("REPLAY_ROOT_INVALID", 422)
            if seq <= session.ack:
                return {"state": "retired", "ack_through": session.ack}
            root = self.root(session, seq)
            self.configure(root, request)
            if operation == "cancel":
                attempt = request.get("attempt")
                if type(attempt) is not int or attempt < 1 or attempt > root.attempt + 1:
                    raise ReadError("REPLAY_ATTEMPT_INVALID", 422)
                root.canceled_through = max(root.canceled_through, attempt)
                root.attempt = max(root.attempt, attempt)
            elif operation in {"seal", "ack"}:
                root.sealed = True
            elif operation != "status":
                raise ReadError("REPLAY_CONTROL_INVALID", 422)
            stopped = root.active == 0
            result = {
                "state": "settled" if root.sealed and stopped else "stopped" if stopped else "stopping",
                "root_seq": seq,
                "canceled_through": root.canceled_through,
                "bytes": root.bytes,
                "index_steps": root.index_steps,
                "index_bytes": root.index_bytes,
                "session_index_bytes": session.total_index_bytes,
                "account_index_bytes": self.accounts[account].total_index_bytes,
                "session_bytes": session.total_bytes,
                "account_bytes": self.accounts[account].total_bytes,
                "raw_bytes": root.raw_bytes,
                "session_raw_bytes": session.raw_bytes,
                "account_raw_bytes": self.accounts[account].raw_bytes,
            }
            if operation == "ack" and root.sealed and stopped:
                root.pins.clear()
                session.ack, session.root = seq, None
            return result

    def admit(self, account: int, execution: str, request: dict, credit: int) -> tuple[ReadSession, Root]:
        """在访问索引之前预留峰值、速率和根任务额度。"""
        with self.lock:
            if self.maintaining:
                raise ReadError("REPLAY_READ_WAIT", 429)
            self._expire()
            session = self.session(account, execution, request)
            root = self.root(session, request.get("root_seq"))
            self.configure(root, request)
            attempt = request.get("attempt")
            # All fences run before SQLite opens or numeric block IDs are interpreted.
            if root.sealed or type(attempt) is not int or attempt <= root.canceled_through:
                raise ReadError("REPLAY_ATTEMPT_RETIRED")
            if attempt != root.attempt:
                if root.active or attempt != root.attempt + 1 or root.attempt > root.canceled_through:
                    raise ReadError("REPLAY_ATTEMPT_INVALID")
                root.attempt = attempt
            if self.now() - root.started >= root.seconds:
                raise ReadError("REPLAY_ROOT_DEADLINE", 408)
            owner = self.accounts[account]
            owner.buckets = {s: b for s, b in owner.buckets.items() if self.now() - s < WINDOW + 1}
            owner.step_buckets = {s: n for s, n in owner.step_buckets.items() if self.now() - s < WINDOW + 1}
            if owner.active >= 2 or sum(a.active for a in self.accounts.values()) >= 4:
                raise ReadError("REPLAY_READ_WAIT", 429)
            if sum(owner.buckets.values()) + credit > WINDOW_BYTES:
                raise ReadError("REPLAY_READ_RATE", 429)
            reserve_steps = min(REQUEST_INDEX_STEPS, max(0, root.step_limit - root.index_steps))
            if sum(owner.step_buckets.values()) + reserve_steps > WINDOW_INDEX_STEPS:
                raise ReadError("REPLAY_INDEX_RATE", 429)
            if root.bytes + credit > root.byte_limit or reserve_steps < 100:
                raise ReadError("REPLAY_ROOT_BUDGET", 413)
            if root.active:
                raise ReadError("REPLAY_READ_CONCURRENCY", 429)
            root.active += 1
            owner.active += 1
            root.bytes += credit
            owner.total_bytes += credit
            session.total_bytes += credit
            slot = int(self.now())
            root.reserved_slot = slot
            root.reserved_steps = reserve_steps
            owner.buckets[slot] = owner.buckets.get(slot, 0) + credit
            owner.step_buckets[slot] = owner.step_buckets.get(slot, 0) + reserve_steps
            return session, root

    def check(self, session: ReadSession, root: Root, attempt: int) -> None:
        """在有界读取步骤间检查取消和期限。"""
        with self.lock:
            if (
                root.sealed
                or attempt <= root.canceled_through
                or self.now() >= session.expires
                or self.now() - root.started >= root.seconds
            ):
                raise ReadError("REPLAY_READ_CANCELED")

    def release(
        self, session: ReadSession, root: Root, credit: int, actual: int, steps: int, raw_bytes=0, index_bytes=0
    ) -> None:
        """实际工作停止后替换预留；累计原始读取和索引工作分开记录。"""
        with self.lock:
            owner = self.accounts[session.account]
            root.active -= 1
            owner.active -= 1
            # Replace the reservation only after the indexed read and actual send stop.
            if root.reserved_slot in owner.buckets:
                owner.buckets[root.reserved_slot] += actual - credit
            if root.reserved_slot in owner.step_buckets:
                owner.step_buckets[root.reserved_slot] += steps - root.reserved_steps
            root.bytes += actual - credit
            session.total_bytes += actual - credit
            owner.total_bytes += actual - credit
            root.index_bytes += index_bytes
            session.total_index_bytes += index_bytes
            owner.total_index_bytes += index_bytes
            root.index_steps += steps
            session.total_index_steps += steps
            owner.total_index_steps += steps
            root.raw_bytes += raw_bytes
            session.raw_bytes += raw_bytes
            owner.raw_bytes += raw_bytes


readers = Readers()


@contextlib.contextmanager
def index(root: str | Path, account: int, cancel):
    """单请求固定索引事务，限制 SQLite 指令数和持有时间。"""
    directory = Path(root).resolve() / f"account-{account}"
    path = directory / ".replay-v2.sqlite"
    if directory.is_symlink() or path.is_symlink() or directory.exists() and not directory.is_dir():
        raise ReadError("REPLAY_PATH")
    steps = [0]
    if not path.exists():
        yield None, directory, steps
        return
    from axile.server._native import _core
    from axile.server.execution_replay.raw_store import RawIndex

    db = None
    started = time.monotonic()
    interrupted = None

    def progress():
        nonlocal interrupted
        steps[0] += 100
        try:
            cancel()
            if steps[0] > REQUEST_INDEX_STEPS:
                raise ReadError("REPLAY_INDEX_BUDGET", 413)
            if time.monotonic() - started > 2:
                raise ReadError("REPLAY_INDEX_DEADLINE", 408)
        except ReadError as error:
            interrupted = error
            return 1
        return 0

    try:
        cancel()
        db = RawIndex(path, progress)
        yield db, directory, steps
    except _core.NativeError as error:
        if interrupted is not None:
            raise interrupted from error
        raise ReadError("REPLAY_INDEX_READ", 503) from error
    finally:
        if db is not None:
            db.close()


def metadata(db, request: dict, work=lambda amount, read_bytes=0: None) -> dict:
    """仅返回直接位置和有限目录，不解释原始业务载荷。"""
    if db is None:
        if request.get("operation") == "manifest":
            return {"schema_version": 2, "index_generation": "unrecorded", "recordings": []}
        raise ReadError("REPLAY_SOURCE_VIEW")
    layout = db.one("layout", [])
    if layout is None or layout[0] != "7":
        raise ReadError("REPLAY_INDEX_REBUILD_REQUIRED")
    generation = db.one("generation", [])[0]
    if request.get("operation") == "manifest":
        rows = db.rows("execution_recordings", [request["execution_id"]])
        if len(rows) > 64:
            raise ReadError("REPLAY_MANIFEST_BUDGET", 413)
        recordings = []
        catalog_bytes = 256
        for row in rows:
            start = db.one("recording_start", [row["id"]])
            streams = [dict(s) for s in db.rows("recording_streams", [row["id"], row["seq"]])]
            symbols = [s[0] for s in db.rows("recording_symbols", [row["id"], row["seq"]])]
            recordings.append(
                {
                    **{key: row[key] for key in row.keys() if key != "context"},
                    "context": json.loads(row["context"]) if "context" in row.keys() else {},
                    "start_us": start[0] if start else None,
                    "streams": streams,
                    "symbols": symbols[:128],
                    "symbols_more": len(symbols) > 128,
                }
            )
            catalog_bytes += len(json.dumps(recordings[-1], ensure_ascii=False).encode())
            if catalog_bytes > METADATA_CREDIT:
                raise ReadError("REPLAY_METADATA_BUDGET", 413)
        return {"schema_version": 2, "index_generation": generation, "recordings": recordings}
    if generation != request.get("index_generation"):
        raise ReadError("REPLAY_INDEX_GENERATION")
    recording, through = request.get("recording"), request.get("through_seq")
    if (
        not isinstance(recording, str)
        or not 1 <= len(recording) <= 128
        or not all(c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in recording)
    ):
        raise ReadError("REPLAY_SOURCE_VIEW", 422)
    row = db.one("execution_recording", [recording, recording, request["execution_id"]])
    if row is None or type(through) is not int or not 0 <= through <= row["seq"]:
        raise ReadError("REPLAY_SOURCE_VIEW")
    commit = request.get("commit_id")
    if not isinstance(commit, str) or len(commit) != 64:
        raise ReadError("REPLAY_SOURCE_COMMIT", 422)
    frozen = db.one("committed_view", [recording, commit])
    if frozen is None or through != frozen["seq"]:
        raise ReadError("REPLAY_SOURCE_COMMIT")
    row = frozen
    if request.get("operation") == "display-blocks":
        from axile.server.execution_replay.raw_range import display_blocks

        return {
            **display_blocks(db, request),
            "recording_id": recording,
            "through_seq": through,
            "commit_id": commit,
            "index_generation": generation,
        }
    if request.get("operation") in {"range-blocks", "business-blocks"}:
        from axile.server.execution_replay.raw_range import locations

        return {
            **locations(db, request, work),
            "index_generation": generation,
            "recording_id": recording,
            "through_seq": through,
            "commit_id": commit,
        }
    if request.get("operation", "").startswith("market-"):
        from axile.server.execution_replay.raw_market import locations

        return {
            **locations(db, request),
            "index_generation": generation,
            "recording_id": recording,
            "through_seq": through,
            "commit_id": commit,
        }
    if request.get("operation") in {
        "objects",
        "object-streams",
        "object-blocks",
        "members",
        "commands",
        "queries",
        "versions",
        "dependency-boundary",
        "object-links",
    }:
        from axile.server.execution_replay.raw_index import locations

        result = locations(db, request)
        return {
            **result,
            "index_generation": generation,
            "recording_id": recording,
            "through_seq": through,
            "commit_id": commit,
        }
    if request.get("operation") == "plan":
        target, cutoff = request.get("time_us"), request.get("capture_seq", 9_007_199_254_740_991)
        stream = request.get("stream")
        if (
            type(target) is not int
            or not 0 <= target <= row["watermark"]
            or type(cutoff) is not int
            or not 0 <= cutoff <= 9_007_199_254_740_991
            or type(stream) is not int
            or not 0 <= stream <= 65535
        ):
            raise ReadError("REPLAY_TARGET_INVALID", 422)
        from axile.server.execution_replay.raw_locations import at_sequence, locate

        selected, evidence = locate(db, recording, through, target, cutoff, stream, work)
        ids = sorted({r["block"] for r in evidence} | ({selected["block"]} if selected else set()))
        blocks = [dict(db.one("block", [recording, bid])) for bid in ids]
        cutoff_seq = evidence[0]["seq"] if evidence else 0
        previous_seq = cutoff_seq - 1 if evidence and evidence[0]["time"] == target else cutoff_seq
        previous = at_sequence(db, recording, through, previous_seq, work)
        following = at_sequence(db, recording, through, cutoff_seq + 1, work)
        # Preview is optional evidence, and must never make a valid large-event seek fail.
        while sum(b["length"] for b in blocks) > 4 * 1024 * 1024 and evidence:
            evidence.pop()
            retained = {r["block"] for r in evidence} | ({selected["block"]} if selected else set())
            blocks = [b for b in blocks if b["id"] in retained]
        return {
            "index_generation": generation,
            "recording_id": recording,
            "through_seq": through,
            "commit_id": commit,
            "cutoff_seq": cutoff_seq,
            "previous": previous,
            "next": following,
            "selected": dict(selected) if selected else None,
            "evidence": [dict(r) for r in evidence],
            "blocks": blocks,
            "planned_bytes": sum(b["length"] for b in blocks),
            "target_us": target,
        }
    if request.get("operation") == "blocks":
        from axile.server.execution_replay.raw_index import integer

        ids = request.get("blocks")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 128:
            raise ReadError("REPLAY_BLOCK_BATCH", 422)
        for identity in ids:
            integer(identity, minimum=1, maximum=0xFFFFFFFF)
        if len(set(ids)) != len(ids):
            raise ReadError("REPLAY_BLOCK_BATCH", 422)

        rows = [dict(row) for row in db.rows("frozen_blocks", [recording, *ids, through])]
        if len(rows) != len(ids):
            raise ReadError("REPLAY_BLOCK_INVALID")
        by_id = {row["id"]: row for row in rows}
        ordered = [by_id[identity] for identity in ids]
        if sum(row["length"] for row in ordered) > MAX_BATCH:
            raise ReadError("REPLAY_BLOCK_BATCH", 413)
        return ordered
    block_id = request.get("block")
    if type(block_id) is not int or not 1 <= block_id <= 9_007_199_254_740_991:
        raise ReadError("REPLAY_BLOCK_INVALID", 422)
    block = db.one("frozen_block", [recording, block_id, through])
    if block is None:
        raise ReadError("REPLAY_BLOCK_INVALID")
    return dict(block)


def read(root_path: str | Path, account: int, execution: str, request: dict):
    """返回有界响应及幂等结算函数；发送结束前保持并发额度。"""
    request = {**request, "execution_id": execution}
    is_batch = request.get("operation") == "blocks"
    is_block = request.get("operation") == "block" or is_batch
    credit = MAX_PACKED if is_batch else MAX_BLOCK if is_block else METADATA_CREDIT
    manager = readers
    session, root = manager.admit(account, execution, request, credit)
    actual, raw_actual, index_bytes, steps = 0, 0, 0, [0]
    held = False
    released = False

    def release():
        nonlocal released
        with manager.lock:
            if not released:
                manager.release(session, root, credit, actual + index_bytes, steps[0] + 100, raw_actual, index_bytes)
                released = True

    try:

        def check():
            manager.check(session, root, request["attempt"])
            if root.index_steps + steps[0] + 100 > root.step_limit:
                raise ReadError("REPLAY_ROOT_INDEX_BUDGET", 413)
            if steps[0] + 100 > root.reserved_steps:
                raise ReadError("REPLAY_INDEX_BUDGET", 413)

        if manager._owner is None or str(Path(root_path).resolve()) != manager._owner_root:
            raise ReadError("REPLAY_READ_OWNER_REQUIRED", 503)
        recording = request.get("recording")
        if recording is not None:
            if (
                not isinstance(recording, str)
                or not 1 <= len(recording) <= 128
                or not all(c.isascii() and (c.isalnum() or c in "_-") for c in recording)
            ):
                raise ReadError("REPLAY_SOURCE_VIEW", 422)
            with manager.lock:
                if recording not in root.pins:
                    if (
                        len(root.pins) >= 64
                        or sum(len(s.root.pins) for s in manager.sessions.values() if s.root) >= 128
                    ):
                        raise ReadError("REPLAY_SOURCE_VIEW_BUDGET", 413)
                    from axile.server._native import _core

                    root.pins[recording] = _core.ReplayReadLease(str(root_path), account, recording)

        def work(amount, read_bytes=0):
            nonlocal index_bytes
            index_bytes += read_bytes
            steps[0] += amount
            check()

        with index(root_path, account, check) as (db, directory, steps):
            result = metadata(db, request, work)
            check()
        check()
        if not is_block:
            result = {**result, "account_id": account, "execution_id": execution}
            actual = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
            if actual + index_bytes > METADATA_CREDIT:
                raise ReadError("REPLAY_METADATA_BUDGET", 413)
            held = True
            check.usage = lambda: {
                "bytes": root.bytes - credit + actual + index_bytes,
                "index_steps": root.index_steps + steps[0] + 100,
                "session_bytes": session.total_bytes - credit + actual + index_bytes,
            }
            return result, release, check
        rows = result if is_batch else [result]
        for row in rows:
            if not 0 < row["length"] <= MAX_BLOCK or not 0 < row["raw_length"] <= MAX_RAW:
                raise ReadError("REPLAY_BLOCK_BUDGET", 413)
        # Only server-constructed names and indexed offsets reach the file boundary.
        recording = request["recording"]
        if (
            not isinstance(recording, str)
            or not recording
            or len(recording) > 128
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in recording)
        ):
            raise ReadError("REPLAY_RECORDING_INVALID", 422)
        path = directory / f"raw-{recording}.mcap"
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            payload = bytearray(b"AR02" + struct.pack("<I", len(rows))) if is_batch else bytearray()
            for row in rows:
                check()
                part = os.pread(fd, row["length"], row["offset"])
                raw_actual += len(part)
                actual = raw_actual + (8 + len(rows) * 8 if is_batch else 0)
                check()
                if len(part) != row["length"]:
                    raise ReadError("REPLAY_BLOCK_TRUNCATED")
                if is_batch:
                    payload.extend(struct.pack("<II", row["id"], len(part)))
                payload.extend(part)
        finally:
            os.close(fd)
        held = True
        check.usage = lambda: {
            "bytes": root.bytes - credit + actual + index_bytes,
            "index_steps": root.index_steps + steps[0] + 100,
            "session_bytes": session.total_bytes - credit + actual + index_bytes,
        }
        return bytes(payload), release, check
    finally:
        if not held:
            release()
