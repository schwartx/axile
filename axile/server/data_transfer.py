"""执行数据传送：离线包与联网传送共享一致性快照和接收确认。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
from pydantic import BaseModel, Field, model_validator

from axile.server.core.db import database_path
from axile.server.native_store import native_operation_sync

MAX_BYTES = 4 * 1024**3
_lock = asyncio.Lock()
_settings_lock = threading.RLock()


class TransferPlan(BaseModel):
    """保存一次传送的范围、目标和保留规则。"""

    start: datetime | None = None
    end: datetime | None = None
    destination: str = ""
    token: str = ""
    every_hours: int = Field(default=0, ge=0, le=8760)
    execution_days: int | None = Field(default=None, ge=1)
    replay_days: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self):
        """验证时间范围、授权与定时目标。"""
        for value in (self.start, self.end):
            if value is not None and value.tzinfo is None:
                raise ValueError("时间必须带时区")
        if self.start and self.end and self.start >= self.end:
            raise ValueError("结束时间必须晚于开始时间")
        if self.destination:
            url = httpx.URL(self.destination)
            if url.scheme not in {"https", "http"} or not url.host or url.userinfo or url.query or url.fragment:
                raise ValueError("接收端地址无效")
            if not self.token:
                raise ValueError("请填写接收授权")
        if self.every_hours and not self.destination:
            raise ValueError("定时传送需要联网接收端")
        return self


def root() -> Path:
    """返回本实例私有传送目录。"""
    path = Path(database_path()).resolve().parent / "transfer"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def write_json(path: Path, value: object) -> None:
    """以原子替换保存私有状态。"""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def settings() -> dict:
    """读取或初始化本机传送身份和接收授权。"""
    with _settings_lock:
        path = root() / "settings.json"
        if not path.exists():
            write_json(
                path, {"source_id": secrets.token_hex(16), "receive_token": secrets.token_urlsafe(32), "plan": {}}
            )
        return json.loads(path.read_text())


def save_plan(plan: TransferPlan) -> None:
    """持久化传送规则。"""
    value = settings()
    value["plan"] = plan.model_dump(mode="json")
    write_json(root() / "settings.json", value)


def digest(path: Path) -> str:
    """流式计算文件哈希。"""
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def recording_files(plan: TransferPlan) -> list[Path]:
    """选择时间范围内已封口的录制段。"""
    replay = Path(database_path()).resolve().parent / "replay"
    end = (plan.end or datetime.now(UTC)).astimezone(UTC).date().isoformat()
    start = plan.start.astimezone(UTC).date().isoformat() if plan.start else "0000-00-00"
    # 包含边界日的全部已封口段；共享片段不能按执行 ID 裁剪。
    return sorted(
        p
        for p in replay.glob("account-*/*/*.mcap")
        if not p.is_symlink() and (p.name.startswith("fragment-") or start <= p.parent.name <= end)
    )


def export_package(plan: TransferPlan) -> tuple[Path, dict]:
    """生成一致性快照与不可变录制数据包。"""
    batch = secrets.token_hex(16)
    directory = root() / "outbox" / batch
    directory.mkdir(parents=True, mode=0o700)
    try:
        end = plan.end or datetime.now(UTC)
        if end > datetime.now(UTC):
            raise ValueError("截止时间不能晚于当前时间")
        snapshot = directory / "database.sqlite3"
        report = native_operation_sync(
            "transfer_data",
            {
                "action": "export",
                "snapshot": str(snapshot),
                "start_us": int(plan.start.timestamp() * 1_000_000) if plan.start else 0,
                "end_us": int(end.timestamp() * 1_000_000),
            },
            database=database_path(),
        )
        base = Path(database_path()).resolve().parent
        files = {"database.sqlite3": snapshot}
        for index in (base / "replay").glob("account-*/.replay-index.sqlite"):
            name = str(index.relative_to(base))
            copy = directory / name
            copy.parent.mkdir(parents=True, exist_ok=True)
            native_operation_sync(
                "transfer_data", {"action": "backup_index", "snapshot": str(copy)}, database=str(index)
            )
            files[name] = copy
        files.update({str(p.relative_to(base)): p for p in recording_files(plan)})
        from axile.server.app import get_algorithm_directories

        for number, configured in enumerate(get_algorithm_directories()):
            code = configured.expanduser().resolve()
            for path in code.rglob("*"):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(code).parts)
                ):
                    files[f"code/{number}/{path.relative_to(code).as_posix()}"] = path
        if sum(p.stat().st_size for p in files.values()) > MAX_BYTES:
            raise ValueError("数据包超过预算，请缩短范围")
        if shutil.disk_usage(base).free < sum(p.stat().st_size for p in files.values()) + 256 * 1024**2:
            raise ValueError("剩余磁盘不足以暂存数据包，源数据未清理")
        manifest = {
            "format": 1,
            "batch": batch,
            "source_id": settings()["source_id"],
            "start": plan.start.isoformat() if plan.start else None,
            "end": end.isoformat(),
            "executions": report["executions"],
            "files": {n: {"sha256": digest(p), "size": p.stat().st_size} for n, p in files.items()},
        }
        package = directory / "data.axile"
        with ZipFile(package, "w", ZIP_DEFLATED) as archive:
            for name, path in files.items():
                archive.write(path, name)
            archive.writestr("manifest.json", json.dumps(manifest))
        write_json(directory / "manifest.json", manifest)
        snapshot.unlink()
        shutil.rmtree(directory / "replay", ignore_errors=True)
        return package, manifest
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def validate_manifest(manifest: dict) -> None:
    """验证包身份、预算与相对路径。"""
    if manifest.get("format") != 1 or not manifest.get("files") or "database.sqlite3" not in manifest["files"]:
        raise ValueError("不支持的数据包")
    for key in ("batch", "source_id"):
        value = manifest.get(key, "")
        if len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("无效数据包身份")
    if len(manifest["files"]) > 100_000 or sum(f["size"] for f in manifest["files"].values()) > MAX_BYTES:
        raise ValueError("数据包超过接收预算，请缩短范围")
    for name, info in manifest["files"].items():
        p = Path(name)
        index_file = (
            len(p.parts) == 3
            and p.parts[0] == "replay"
            and p.parts[1].startswith("account-")
            and p.name == ".replay-index.sqlite"
        )
        code_file = len(p.parts) >= 3 and p.parts[0] == "code" and p.parts[1].isdigit()
        if (
            name != "database.sqlite3"
            and not index_file
            and not code_file
            and (
                len(p.parts) != 4
                or p.parts[0] != "replay"
                or not p.parts[1].startswith("account-")
                or p.suffix != ".mcap"
            )
        ):
            raise ValueError("无效数据包路径")
        if p.is_absolute() or ".." in p.parts or "\\" in name or info["size"] < 0:
            raise ValueError("无效数据包路径")


def import_package(package: Path) -> dict:
    """完整校验后累积接收同源数据，签发接收确认。"""
    base = Path(database_path()).resolve().parent
    with tempfile.TemporaryDirectory(dir=root(), prefix="receive-") as temporary:
        directory = Path(temporary)
        with ZipFile(package) as archive:
            if len(archive.infolist()) > 100_001 or archive.getinfo("manifest.json").file_size > 16 * 1024**2:
                raise ValueError("数据包超过预算")
            manifest = json.loads(archive.read("manifest.json"))
            validate_manifest(manifest)
            if sorted(archive.namelist()) != sorted([*manifest["files"], "manifest.json"]):
                raise ValueError("数据包文件清单不一致")
            for name, info in manifest["files"].items():
                if archive.getinfo(name).file_size != info["size"]:
                    raise ValueError("文件大小与清单不符")
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                if digest(target) != info["sha256"]:
                    raise ValueError("文件校验失败")
        # 所有文件先验证；已存在的不可变录制不能被覆盖。
        for name, info in manifest["files"].items():
            if name == "database.sqlite3":
                continue
            target = (
                base / name
                if not name.startswith("code/")
                else root() / "algorithms" / manifest["source_id"] / manifest["batch"] / Path(name).relative_to("code")
            )
            if any(p.is_symlink() for p in (target, *target.parents)):
                raise ValueError("接收路径不能包含符号链接")
            if target.exists() and target.name != ".replay-index.sqlite" and digest(target) != info["sha256"]:
                raise ValueError("同名录制内容冲突")
        native_operation_sync(
            "transfer_data",
            {"action": "import", "snapshot": str(directory / "database.sqlite3"), "source_id": manifest["source_id"]},
            database=database_path(),
        )
        for name in manifest["files"]:
            if name != "database.sqlite3":
                target = (
                    base / name
                    if not name.startswith("code/")
                    else root()
                    / "algorithms"
                    / manifest["source_id"]
                    / manifest["batch"]
                    / Path(name).relative_to("code")
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.name == ".replay-index.sqlite":
                    native_operation_sync(
                        "transfer_data",
                        {"action": "import_index", "snapshot": str(directory / name)},
                        database=str(target),
                    )
                elif not target.exists():
                    (directory / name).replace(target)
        code_roots = sorted(
            {
                str((root() / "algorithms" / manifest["source_id"] / manifest["batch"] / Path(n).parts[1]).resolve())
                for n in manifest["files"]
                if n.startswith("code/")
            }
        )
        if code_roots:
            from axile.common.config import settings as app_settings
            from axile.common.config import update_config_toml_values

            update_config_toml_values(
                {
                    "algorithm_directories": list(
                        dict.fromkeys(
                            [
                                *(
                                    d
                                    for d in app_settings.algorithm_directories
                                    if not Path(d).is_relative_to(root() / "algorithms" / manifest["source_id"])
                                ),
                                *code_roots,
                            ]
                        )
                    )
                }
            )
        # 只有数据库与所有录制都已落盘，才签发接收确认。
        receipt = {
            "batch": manifest["batch"],
            "source_id": manifest["source_id"],
            "manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        }
        write_json(root() / f"receipt-{manifest['batch']}.json", receipt)
        return receipt


def acknowledge(receipt: dict, plan: TransferPlan) -> dict:
    """验证确认并按保留规则清理已传送数据。"""
    batch = receipt.get("batch", "")
    if len(batch) != 32 or any(c not in "0123456789abcdef" for c in batch):
        raise ValueError("无效接收确认")
    directory = root() / "outbox" / batch
    manifest = json.loads((directory / "manifest.json").read_text())
    expected = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if receipt.get("manifest_sha256") != expected or receipt.get("source_id") != settings()["source_id"]:
        raise ValueError("接收确认与本机数据包不符")
    write_json(directory / "receipt.json", receipt)
    result = {"removed_executions": 0, "removed_recordings": 0}
    now = datetime.now(UTC)
    if plan.execution_days:
        result.update(
            native_operation_sync(
                "transfer_data",
                {
                    "action": "prune",
                    "cutoff_us": int((now - timedelta(days=plan.execution_days)).timestamp() * 1_000_000),
                    "executions": manifest["executions"],
                },
                database=database_path(),
            )
        )
    if plan.replay_days:
        result["removed_recordings"] = prune_recordings(manifest, now - timedelta(days=plan.replay_days))
    (directory / "data.axile").unlink(missing_ok=True)
    return result


def prune_recordings(manifest: dict, cutoff: datetime) -> int:
    """完整确认整份录制且无活跃/跨录制引用后才回收文件和索引。"""
    base = Path(database_path()).resolve().parent
    by_recording: dict[tuple[Path, str], list[Path]] = {}
    for name in manifest["files"]:
        path = base / name
        if path.name.startswith("recording-"):
            recording = path.stem.removeprefix("recording-").rsplit("-", 1)[0]
            by_recording.setdefault((path.parent.parent, recording), []).append(path)
    removed = 0
    for (account, recording), _ in by_recording.items():
        if any(account.glob("*/*.partial")):
            continue
        files = [*account.glob(f"*/recording-{recording}-*.mcap"), *account.glob(f"*/fragment-{recording}-*.mcap")]
        if not files:
            continue
        if any(p.parent.name >= cutoff.date().isoformat() or p.is_symlink() for p in files):
            continue
        if any(
            str(p.relative_to(base)) not in manifest["files"]
            or digest(p) != manifest["files"][str(p.relative_to(base))]["sha256"]
            for p in files
        ):
            continue
        safe = native_operation_sync(
            "transfer_data",
            {"action": "prune_recording", "index": str(account / ".replay-index.sqlite"), "recording": recording},
            database=database_path(),
        )
        if not safe["safe"]:
            continue
        for file in files:
            file.unlink()
            removed += 1
    return removed


async def send(plan: TransferPlan) -> dict:
    """发送数据并在接收确认后执行清理。"""
    async with _lock:
        pending = root() / "pending.json"
        key = hashlib.sha256(json.dumps(plan.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
        previous = json.loads(pending.read_text()) if pending.exists() else None
        if previous and previous["key"] == key:
            directory = root() / "outbox" / previous["batch"]
            package = directory / "data.axile"
            manifest = json.loads((directory / "manifest.json").read_text())
        else:
            package, manifest = await asyncio.to_thread(export_package, plan)
            write_json(pending, {"key": key, "batch": manifest["batch"]})
        async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
            peer = await client.get(
                plan.destination.rstrip("/") + "/api/v1/transfer/identity", headers={"x-transfer-token": plan.token}
            )
            peer.raise_for_status()
            if peer.json()["source_id"] == settings()["source_id"]:
                raise ValueError("接收端不能是本机")
            with package.open("rb") as file:
                response = await client.post(
                    plan.destination.rstrip("/") + "/api/v1/transfer/receive",
                    headers={"x-transfer-token": plan.token},
                    files={"file": (package.name, file, "application/octet-stream")},
                )
            response.raise_for_status()
            receipt = response.json()
        result = await asyncio.to_thread(acknowledge, receipt, plan)
        pending.unlink(missing_ok=True)
        return {"batch": manifest["batch"], **result}


async def scheduled_send() -> None:
    """执行定时传送并保存脱敏结果。"""
    try:
        result = await send(TransferPlan.model_validate(settings()["plan"]))
        write_json(root() / "last-run.json", {"ok": True, "at": datetime.now(UTC).isoformat(), "result": result})
    except Exception:
        # 凭据和账户配置不进入调度错误日志。
        write_json(
            root() / "last-run.json",
            {"ok": False, "at": datetime.now(UTC).isoformat(), "error": "传送失败，源数据保留，请检查接收端与授权"},
        )


def configure_schedule() -> None:
    """恢复或撤销本实例的定时传送任务。"""
    from axile.server.core.scheduler import scheduler

    if scheduler.get_job("data-transfer"):
        scheduler.remove_job("data-transfer")
    plan = TransferPlan.model_validate(settings()["plan"])
    if plan.every_hours:
        scheduler.add_job(
            scheduled_send, "interval", hours=plan.every_hours, id="data-transfer", max_instances=1, coalesce=True
        )
