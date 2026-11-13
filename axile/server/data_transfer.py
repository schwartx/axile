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
    """向数据层传递范围和保留规则，不传联网授权。"""
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


async def send(plan: TransferPlan) -> dict:
    """由 Rust 准备或恢复批次，Python 只执行联网交付。"""
    async with _lock:
        key = hashlib.sha256(json.dumps(plan.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
        batch = await native_call(
            _operation, "prepare_send", key=key, algorithm_directories=_algorithm_directories(), **_plan_request(plan)
        )
        receipt = batch["receipt"]
        if receipt is None:
            async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
                peer = await client.get(
                    plan.destination.rstrip("/") + "/api/v1/transfer/identity", headers={"x-transfer-token": plan.token}
                )
                peer.raise_for_status()
                if peer.json()["source_id"] == settings()["source_id"]:
                    raise ValueError("接收端不能是本机")
                with Path(batch["package"]).open("rb") as file:
                    response = await client.post(
                        plan.destination.rstrip("/") + "/api/v1/transfer/receive",
                        headers={"x-transfer-token": plan.token},
                        files={"file": ("data.axile", file, "application/octet-stream")},
                    )
                response.raise_for_status()
                receipt = response.json()
        result = await native_call(acknowledge, receipt, plan)
        return {"batch": batch["batch"], **result}


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
