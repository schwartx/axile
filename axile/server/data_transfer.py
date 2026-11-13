"""执行数据传送：离线包与联网传送共享一致性快照和接收确认。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, model_validator

from axile.server.core.db import database_path
from axile.server.native_store import native_call, native_operation_sync

MAX_BYTES = 4 * 1024**3
_lock = asyncio.Lock()
_settings_lock = threading.RLock()


class TransferPlan(BaseModel):
    """保存一次传送的范围、来源和源端保留规则。"""

    start: datetime | None = None
    end: datetime | None = None
    source_url: str = ""
    every_hours: int = Field(default=0, ge=0, le=8760)
    execution_days: int | None = Field(default=None, ge=1)
    replay_days: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self):
        """验证时间范围与定时目标。"""
        for value in (self.start, self.end):
            if value is not None and value.tzinfo is None:
                raise ValueError("时间必须带时区")
        if self.start and self.end and self.start >= self.end:
            raise ValueError("结束时间必须晚于开始时间")
        if self.source_url:
            try:
                url = httpx.URL(self.source_url)
            except httpx.InvalidURL:
                raise ValueError("来源端地址无效") from None
            if url.scheme not in {"https", "http"} or not url.host or url.userinfo or url.query or url.fragment:
                raise ValueError("来源端地址无效")
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError("来源端端口无效")
        if self.every_hours and not self.source_url:
            raise ValueError("定时拉取需要来源端地址")
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
    """读取或初始化本机传送身份。"""
    with _settings_lock:
        path = root() / "settings.json"
        if not path.exists():
            write_json(path, {"source_id": secrets.token_hex(16), "plan": {}})
        return json.loads(path.read_text())


def save_plan(plan: TransferPlan) -> None:
    """持久化传送规则。"""
    value = settings()
    value["plan"] = plan.model_dump(mode="json")
    write_json(root() / "settings.json", value)


def _operation(action: str, **request: object) -> dict:
    """调用完整原生阶段，并保留数据包校验的 ValueError 语义。"""
    from axile.server._native import _core

    try:
        return native_operation_sync("transfer_data", {"action": action, **request}, database=database_path())
    except _core.NativeError as error:
        if error.args[0] == "TRANSFER_PACKAGE":
            raise ValueError(error.args[1]) from None
        raise


def _plan_request(plan: TransferPlan) -> dict:
    """向数据层传递范围和保留规则。"""
    return {
        "source_id": settings()["source_id"],
        "plan": plan.model_dump(mode="json", include={"start", "end", "execution_days", "replay_days"}),
    }


def _algorithm_directories() -> list[str]:
    """读取应用实际加载的算法目录。"""
    from axile.server.app import get_algorithm_directories

    return [str(path.expanduser().resolve()) for path in get_algorithm_directories()]


def export_package(plan: TransferPlan) -> tuple[Path, dict]:
    """由 Rust 生成快照、录制和代码的完整数据包。"""
    report = _operation("export_package", algorithm_directories=_algorithm_directories(), **_plan_request(plan))
    return Path(report["package"]), report["manifest"]


def import_package(package: Path) -> dict:
    """由 Rust 校验接收数据，并将收到的代码目录接入应用配置。"""
    report = _operation("import_package", package=str(package))
    code_roots = report["algorithm_directories"]
    if code_roots:
        from axile.common.config import settings as app_settings
        from axile.common.config import update_config_toml_values

        source_root = root() / "algorithms" / report["source_id"]
        update_config_toml_values(
            {
                "algorithm_directories": list(
                    dict.fromkeys(
                        [
                            *(d for d in app_settings.algorithm_directories if not Path(d).is_relative_to(source_root)),
                            *code_roots,
                        ]
                    )
                ),
            }
        )
    return report["receipt"]


def acknowledge(receipt: dict, plan: TransferPlan) -> dict:
    """由 Rust 验证回执并可重复完成源端清理。"""
    return _operation("acknowledge", receipt=receipt, **_plan_request(plan))


def prepare_package(plan: TransferPlan, requester: str) -> Path:
    """复用来源端未确认的批次，清理中断时先完成已有确认。"""
    key = hashlib.sha256(
        json.dumps({"requester": requester, **_plan_request(plan)}, sort_keys=True).encode()
    ).hexdigest()
    request = {"key": key, "algorithm_directories": _algorithm_directories(), **_plan_request(plan)}
    batch = _operation("prepare_package", **request)
    if batch["receipt"] is not None:
        acknowledge(batch["receipt"], plan)
        batch = _operation("prepare_package", **request)
    return Path(batch["package"])


async def check_source(source_url: str) -> dict:
    """只检查来源端身份与连接，不生成数据包或触发清理。"""
    if not source_url:
        raise ValueError("请填写来源端地址")
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.get(source_url.rstrip("/") + "/api/v1/transfer/identity")
            response.raise_for_status()
    except httpx.TimeoutException:
        raise ValueError("检测超时，请检查来源端地址和网络") from None
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"来源端返回 HTTP {exc.response.status_code}，请检查地址及数据传送接口") from None
    except httpx.RequestError:
        raise ValueError("无法连接来源端，请检查地址、端口和网络") from None
    try:
        value = response.json()
    except ValueError:
        value = None
    source_id = value.get("source_id") if isinstance(value, dict) else None
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("来源端未提供有效的数据传送身份，请检查地址和服务版本")
    if source_id == settings()["source_id"]:
        raise ValueError("来源端不能是本机")
    return {"source_id": source_id}


async def pull(plan: TransferPlan) -> dict:
    """主动下载、导入，再向来源端交回可重试的接收确认。"""
    async with _lock:
        pending = root() / "pull-pending.json"
        async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
            if pending.exists():
                delivery = json.loads(pending.read_text())
            else:
                if not plan.source_url:
                    raise ValueError("请填写来源端地址")
                source = plan.source_url.rstrip("/")
                peer = await client.get(source + "/api/v1/transfer/identity")
                peer.raise_for_status()
                if peer.json()["source_id"] == settings()["source_id"]:
                    raise ValueError("来源端不能是本机")
                remote_plan = _plan_request(plan)["plan"]
                with tempfile.TemporaryDirectory(dir=root(), prefix="pull-") as temporary:
                    package = Path(temporary) / "data.axile"
                    async with client.stream(
                        "POST",
                        source + "/api/v1/transfer/export",
                        params={"requester": settings()["source_id"]},
                        json=remote_plan,
                    ) as response:
                        response.raise_for_status()
                        total = 0
                        with package.open("wb") as file:
                            async for data in response.aiter_bytes(1024 * 1024):
                                total += len(data)
                                if total > MAX_BYTES:
                                    raise ValueError("数据包过大，请缩短范围")
                                file.write(data)
                    receipt = await native_call(import_package, package)
                delivery = {"source_url": source, "plan": remote_plan, "receipt": receipt}
                write_json(pending, delivery)
            response = await client.post(
                delivery["source_url"] + "/api/v1/transfer/acknowledge",
                json={"receipt": delivery["receipt"], "plan": delivery["plan"]},
            )
            response.raise_for_status()
            result = {"batch": delivery["receipt"]["batch"], **response.json()}
            pending.unlink()
            return result


async def scheduled_pull() -> None:
    """执行定时拉取并保存结果。"""
    try:
        result = await pull(TransferPlan.model_validate(settings()["plan"]))
        write_json(root() / "last-run.json", {"ok": True, "at": datetime.now(UTC).isoformat(), "result": result})
    except Exception:
        # 凭据和账户配置不进入调度错误日志。
        write_json(
            root() / "last-run.json",
            {
                "ok": False,
                "at": datetime.now(UTC).isoformat(),
                "error": "拉取未完成，源数据仅在确认后清理，请检查来源端",
            },
        )


def configure_schedule() -> None:
    """恢复或撤销本实例的定时传送任务。"""
    from axile.server.core.scheduler import scheduler

    if scheduler.get_job("data-transfer"):
        scheduler.remove_job("data-transfer")
    plan = TransferPlan.model_validate(settings()["plan"])
    if plan.every_hours:
        scheduler.add_job(
            scheduled_pull, "interval", hours=plan.every_hours, id="data-transfer", max_instances=1, coalesce=True
        )
