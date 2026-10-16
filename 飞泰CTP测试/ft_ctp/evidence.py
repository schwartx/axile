"""原子结果文件、批次占用和集中脱敏。"""

import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from axile.executor.algorithms.utils.clock import clock_now_iso

SENSITIVE = re.compile(
    r"password|passwd|secret|token|auth_code|app_id|investor_id|broker_id|user_id|feishu|front$", re.I
)


class Redactor:
    """字段脱敏同时替换错误文字中的已知凭据。"""

    def __init__(self, credentials=None):
        self.secrets = sorted(
            {str(v) for v in (credentials or {}).values() if isinstance(v, str) and v}, key=len, reverse=True
        )

    def __call__(self, value):
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            return {str(k): "[REDACTED]" if SENSITIVE.search(str(k)) else self(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
        return value


class Bundle:
    """已存在的批次拒绝重跑；未知提交也保留占用。"""

    def __init__(self, root: str, batch: str, config: dict, redactor=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", batch):
            raise ValueError("batch 只能使用 1..80 位字母、数字、下划线及短横线")
        self.path = Path(root) / batch
        self.path.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.redact = redactor or Redactor()
        self.index = {
            "schema_version": 1,
            "batch": batch,
            "started_at": clock_now_iso(),
            "config": config,
            "scenarios": [],
            "missing": [],
            "mode": "connected",
        }
        self.write("index.json", self.index)

    def write(self, name, data, *, redact=True):
        """文件以 0600 权限原子替换，提交意图必须先于副作用。"""
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.redact(data) if redact else data, stream, ensure_ascii=False, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        return str(path.relative_to(self.path))

    def finish(self):
        """生成摘要及 SHA256 清单；每个场景保留独立状态。"""
        self.index["finished_at"] = clock_now_iso()
        self.write("index.json", self.index)
        lines = [
            f"# 飞泰 CTP 批次 {self.index['batch']}",
            "",
            f"模式：{self.index['mode']}",
            "",
            "结果来自 Axile 联机调用；各场景按实际回报和材料完整性分别判定。",
            "",
            "| 场景 | 结果 | 说明 |",
            "| --- | --- | --- |",
        ]
        for row in self.index["scenarios"]:
            note = str(row.get("reason", "")).replace("\n", " ").replace("|", "/")
            lines.append(f"| {row['scenario']} | {row['status']} | {note} |")
        lines.extend(["", "## 缺失材料", ""] + [f"- {m}" for m in self.index["missing"]])
        summary = self.path / "摘要.md"
        summary.write_text(self.redact("\n".join(lines)) + "\n")
        summary.chmod(0o600)
        manifest = {
            str(p.relative_to(self.path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.path.rglob("*")
            if p.is_file() and p.name != "manifest.json"
        }
        # 哈希是对已脱敏文件的校验值；再次做凭据子串替换会破坏校验值。
        self.write("manifest.json", manifest, redact=False)


@contextmanager
def account_lock(root, account_id):
    """跨批次锁定账户，进程崩溃由操作系统释放，批次仍不可重跑。"""
    import fcntl

    path = Path(root) / "state"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (path / f"account-{account_id}.lock").open("w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("该账户已有场景脚本运行") from exc
        yield
