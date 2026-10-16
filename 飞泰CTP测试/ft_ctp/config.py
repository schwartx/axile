"""读取非敏感参数及只读 SQLite 凭据。"""

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Config:
    database: str = "../../axile.db"
    account_name: str = "宏源测试"
    api_url: str = "http://127.0.0.1:1419/api/v1"
    symbol: str = "rb2701"
    lots: int = 1
    side: str = "long"
    close_offset: str = "auto"
    max_order_lots: int = 1
    max_exposure_lots: int = 1
    max_requests: int = 12
    repetitions: int = 3
    timeout_seconds: float = 45
    poll_seconds: float = 0.5
    passive_ticks: int = 10
    threshold: int = 3
    daily_limit: int = 6
    warning_percent: int = 50
    pause_point: str = "executing"
    pause_action: str = "terminate"
    pause_checkpoint: str = "ready"
    terminate_mode: str = "graceful"
    observe_seconds: float = 5
    cron_expr: str = "* * * * *"
    supplement: dict | None = None
    invalid_case: str = "price"
    unknown_symbol: str = "FT_UNKNOWN_CONTRACT"
    reject_case: str = "insufficient_funds"
    broker_condition: str = ""
    reject_volume: int = 1
    expected_error_id: int | None = None
    results_dir: str = "results"

    @classmethod
    def read(cls, path: Path | None):
        """路径相对配置文件解析，不依赖启动目录。"""
        source = Path(__file__).resolve().parents[1]
        base = path.resolve().parent if path else source if (source / "run.py").exists() else Path.cwd()
        config = cls(**json.loads(path.read_text()) if path else {})
        if path is None and not (source / "run.py").exists():
            config.database = "axile.db"
        for key in ("database", "results_dir"):
            value = Path(getattr(config, key))
            setattr(config, key, str(value if value.is_absolute() else base / value))
        config.validate()
        return config

    def validate(self):
        """拒绝超限、非整数手数和不明确的场景参数。"""
        positive = (
            "lots",
            "max_order_lots",
            "max_exposure_lots",
            "max_requests",
            "repetitions",
            "passive_ticks",
            "threshold",
            "daily_limit",
            "reject_volume",
        )
        for key in positive:
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"{key} 必须为正整数")
        for key in ("timeout_seconds", "poll_seconds", "observe_seconds"):
            value = getattr(self, key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} 必须为有限正数")
        if self.lots > min(self.max_order_lots, self.max_exposure_lots):
            raise ValueError("手数超过单笔量或总敞口约束")
        if self.repetitions * 2 > self.max_requests or self.max_requests > 100:
            raise ValueError("报撤次数超过 max_requests（上限 100）")
        choices = {
            "pause_checkpoint": {"ready", "before_send", "after_send"},
            "terminate_mode": {"graceful", "cancel_pending"},
            "side": {"long", "short"},
            "close_offset": {"auto", "today", "yesterday"},
            "invalid_case": {"price", "volume", "unknown"},
            "reject_case": {"insufficient_funds", "no_position", "market_state"},
            "pause_point": {"executing", "quota", "queued", "scheduled", "supplement", "idle", "restart"},
            "pause_action": {"terminate", "pause_auto", "cancel_supplement"},
        }
        for key, values in choices.items():
            if getattr(self, key) not in values:
                raise ValueError(f"{key} 不支持: {getattr(self, key)}")
        if self.pause_point != "executing" and self.pause_checkpoint != "ready":
            raise ValueError("发送边界锚点仅用于 pause_point=executing")
        if type(self.warning_percent) is not int or not 1 <= self.warning_percent <= 99:
            raise ValueError("warning_percent 必须在 1..99")
        if not self.symbol or not self.account_name:
            raise ValueError("必须指定账户与合约")

    def public(self):
        """配置只含选择器与测试参数，不含凭据。"""
        return asdict(self)


def load_account(database: str, name: str) -> dict:
    """精确匹配一个 CTP 账户，只读支持新旧两种数据库布局。"""
    path = Path(database).resolve(strict=True)
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM account WHERE name = ?", (name,)).fetchall()
        if len(rows) != 1:
            raise ValueError("账户名称不存在或不唯一")
        account = dict(rows[0])
        if account["trade_channel"] != "ctp":
            raise ValueError("指定账户不是 CTP")
        if "account_config" in account:
            for key in ("account_config", "algorithm", "trade_rules", "account_control_override"):
                if isinstance(account.get(key), str):
                    account[key] = json.loads(account[key])
        else:
            settings = connection.execute(
                "SELECT * FROM account_settings WHERE account_id = ?", (account["id"],)
            ).fetchone()
            if settings is None:
                raise ValueError("账户设置缺失")
            account["account_config"] = json.loads(settings["connection"])
            for group in ("execution", "schedule"):
                account.update(json.loads(settings[group]))
        if not account["account_config"] or not account["account_config"].get("password"):
            raise ValueError("账户登录凭据缺失")
        return account
