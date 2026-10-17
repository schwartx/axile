"""通过 REST 编排场景并复用产品证据导出。"""

import copy
import csv
import hashlib
import io
import json
import math
import os
import zipfile
from importlib.metadata import PackageNotFoundError, version

from axile.executor.algorithms.utils.clock import clock_now_iso, get_default_clock

from ft_ctp.api import ApiError
from ft_ctp.checks import find_core_results, find_results, judge, judge_core_noop, reconcile

TERMINAL = {"SUCCEEDED", "FAILED", "TERMINATED", "SKIPPED", "PARTIAL"}
RESTORE_FIELDS = (
    "portfolio_id",
    "algorithm",
    "trade_rules",
    "long_leverage",
    "short_leverage",
    "weight_precision",
    "account_control_override",
    "is_started",
    "cron_expr",
    "supplement",
    "execution_timeout",
)


class Conditions(RuntimeError):
    """有明确前置不足，不应宣称场景失败或通过。"""


class Runner:
    def __init__(self, config, api, bundle, account_id):
        self.config, self.api, self.bundle, self.account_id = config, api, bundle, account_id
        self.base = f"/account/{account_id}"
        self.clock = get_default_clock()
        self.active_execution = None
        self.requests = 0
        self.original = None
        self.portfolio_id = None
        self.execution_ids = []
        self.pending_execution_ids = set()
        self.preserved_positions = []
        self.preserved_targets = {}
        self.bundle.index.update(mode="connected", account_id=account_id, software_versions=self.versions())

    @staticmethod
    def versions():
        versions = {}
        for package in ("axile", "feitai-ctp-tests"):
            try:
                versions[package] = version(package)
            except PackageNotFoundError:
                versions[package] = "source checkout"
        return versions

    def call(self, method, path, body=None, **kwargs):
        """日志落提交意图，再调用 API，失败不能安全重试写请求。"""
        number = self.bundle.index.setdefault("api_calls", 0) + 1
        self.bundle.index["api_calls"] = number
        name = f"requests/{number:04d}.json"
        entry = {"method": method, "path": path, "body": body, "at": clock_now_iso(), "state": "intent"}
        self.bundle.write(name, entry)
        try:
            response = self.api.request(method, path, body, **kwargs)
            entry.update(state="received", response="binary" if kwargs.get("binary") else response)
            self.bundle.write(name, entry)
            return response
        except Exception as exc:
            entry.update(
                state="unknown" if method != "GET" else "read_failed",
                error=str(exc),
                http_status=getattr(exc, "status", None),
            )
            self.bundle.write(name, entry)
            raise

    def preflight(self):
        """确认服务身份、连接状态和测试边界，保存可恢复配置。"""
        account = self.call("GET", self.base)
        if account["name"] != self.config.account_name or account["trade_channel"] != "ctp":
            raise Conditions("API 账户与数据库选择的账户身份不一致")
        if account.get("is_started") or account.get("has_pending_supplement"):
            raise Conditions("请先在页面暂停自动调度并处理剩余补发")
        algorithms = self.call("GET", "/algorithms")
        if not any(item.get("name") == "FT-CTP-SCENARIO" for item in algorithms):
            raise Conditions("Axile 服务尚未加载 FT-CTP-SCENARIO 插件")
        self.connect()
        activity = self.activity()
        if activity.get("running_execution_id") or activity.get("pending_execution_id"):
            raise Conditions("账户已有活跃执行，拒绝修改其组合与算法配置")
        if self.config.symbol in (account.get("forbidden_symbols") or []) + (account.get("risk_symbols") or []):
            raise Conditions("测试合约受账户禁用／风险保护，不能关闭保护强行测试")
        pending = self.call("GET", self.base + "/orders?refresh=true")
        if not pending.get("complete") or pending.get("data"):
            raise Conditions("账户存在挂单或挂单快照不完整，请人工核对")
        assets = self.call("POST", self.base + "/assets/refresh")
        positions = assets["assets"].get("positions", [])
        if assets["assets"].get("source") in {"error", "assumed", "unavailable"}:
            raise Conditions("资产快照不可信")
        self.preserved_positions = [p for p in positions if p["symbol"] != self.config.symbol and p["volume"]]
        for position in self.preserved_positions:
            if position["symbol"] in (account.get("risk_symbols") or []):
                raise Conditions("无关持仓受风险品种保护，无法保证测试隔离")
            sign = -1 if position["direction"] in {"空头", "SHORT"} else 1
            symbol = position["symbol"]
            self.preserved_targets[symbol] = self.preserved_targets.get(symbol, 0) + sign * position["volume"]
        self.bundle.write("preserved-positions.json", self.preserved_positions)
        self.original = {key: account.get(key) for key in RESTORE_FIELDS}
        self.bundle.write("restore.json", self.original)
        self.bundle.write("preflight-assets.json", assets)

    def activity(self):
        """流控上下文可属于历史操作；活跃交易任务使用 dashboard 的真实槽位。"""
        dashboard = self.call("GET", "/account/dashboard")
        items = [row for row in dashboard.get("data", []) if row.get("account_id") == self.account_id]
        if len(items) != 1:
            raise Conditions("无法从账户在途快照确认执行状态")
        return items[0]

    def connect(self):
        """经 Axile 恢复CTP测试账户连接，登录失败保留实际服务证据。"""
        self.call("GET", "/utils/health-check/")
        account = self.call("GET", self.base)
        if account["name"] != self.config.account_name or account["trade_channel"] != "ctp":
            raise Conditions("API 账户与凭据来源身份不一致")
        connection = self.call("GET", self.base + "/connection")
        if connection.get("status") != "ready":
            if getattr(self, "credentials", None):
                self.call("PATCH", self.base, {"account_config": self.credentials})
            previous_timeout = getattr(self.api, "timeout", None)
            self.api.timeout = 90
            try:
                connection = self.call("POST", self.base + "/connection/reconnect")
            except ApiError:
                try:
                    self.bundle.write("connection.json", self.call("GET", self.base + "/connection"))
                except ApiError:
                    self.bundle.index["missing"].append("连接失败后无法读取连接详情")
                raise
            finally:
                if previous_timeout is not None:
                    self.api.timeout = previous_timeout
        self.bundle.write("connection.json", connection)
        if connection.get("status") != "ready":
            raise Conditions("CTP 会话尚未 ready，请核对 connection.json 中的实际连接错误")
        return connection

    def prepare(self):
        """建立批次专属组合；不更改凭据、不启动自动调度。"""
        portfolio = self.call(
            "POST",
            "/portfolio/",
            {
                "name": f"FT-{self.bundle.index['batch']}",
                "market": "期货",
                "status": "启用",
                "tag": "feitai",
                "description": "飞泰场景测试专用，不得绑定其他账户",
                "custom_calc_py_code": self.target_code(self.config.lots),
            },
        )
        self.portfolio_id = portfolio["id"]
        self.bundle.index["portfolio_id"] = self.portfolio_id
        rules = copy.deepcopy(self.original.get("trade_rules") or {})
        for symbol in self.preserved_targets:
            rules[symbol] = {**rules.get(symbol, {}), "sizing_mode": "lots"}
        existing_max = rules.get(self.config.symbol, {}).get("max_single_order_size")
        if existing_max and existing_max < self.config.max_order_lots:
            raise Conditions("测试单笔上限比已有保护宽松，拒绝放宽保护")
        rules[self.config.symbol] = {
            **rules.get(self.config.symbol, {}),
            "sizing_mode": "lots",
            "max_single_order_size": self.config.max_order_lots,
        }
        patch = {
            "portfolio_id": self.portfolio_id,
            "is_started": False,
            "supplement": None,
            "long_leverage": 1.0,
            "short_leverage": 1.0,
            "weight_precision": 0.01,
            "trade_rules": rules,
            "execution_timeout": math.ceil(self.config.timeout_seconds + 30),
        }
        if getattr(self, "credentials", None):
            patch["account_config"] = self.credentials
        self.call("PATCH", self.base, patch)

    def target_code(self, lots):
        sign = 1 if self.config.side == "long" else -1
        targets = {**self.preserved_targets, self.config.symbol: sign * lots}
        return f"def calculate_portfolio(context):\n    return {targets!r}\n"

    def check_preserved(self, assets):
        """只比对持仓身份与数量，浮动盈亏不应被误当作仓位变化。"""

        def quantities(positions):
            result = {}
            for position in positions:
                if position["symbol"] != self.config.symbol and position["volume"]:
                    key = (position["symbol"], position["direction"])
                    result[key] = result.get(key, 0) + position["volume"]
            return result

        if assets.get("source") in {"error", "assumed", "unavailable"}:
            raise Conditions("无关持仓核对缺少可靠资产快照")
        if quantities(assets.get("positions", [])) != quantities(self.preserved_positions):
            raise Conditions("无关持仓数量发生变化，停止测试并保留证据，不自动反向操作")

    def configure(self, action, step, **overrides):
        pending = self.call("GET", self.base + "/orders?refresh=true")
        if not pending.get("complete") or pending.get("data"):
            raise Conditions("场景开始前账户出现挂单或快照不完整，拒绝触发核心的执行前清理")
        target = 0 if action == "close" else self.config.lots
        self.call("PATCH", f"/portfolio/{self.portfolio_id}", {"custom_calc_py_code": self.target_code(target)})
        params = {
            key: getattr(self.config, key)
            for key in (
                "symbol",
                "lots",
                "max_order_lots",
                "max_exposure_lots",
                "side",
                "close_offset",
                "timeout_seconds",
                "passive_ticks",
                "invalid_case",
                "reject_case",
                "reject_volume",
                "broker_condition",
            )
        }
        params.update(action=action, batch=self.bundle.index["batch"], step=step, **overrides)
        holds = sum(
            float(params.get(key, 0)) for key in ("hold_seconds", "before_send_hold_seconds", "after_send_hold_seconds")
        )
        self.call(
            "PATCH",
            self.base,
            {
                "algorithm": {"method": "FT-CTP-SCENARIO", "params": params},
                "execution_timeout": math.ceil(self.config.timeout_seconds + holds + 30),
            },
        )
        snapshot = self.call("POST", self.base + "/target_snapshot/refresh")
        self.bundle.write(f"{step}/target.json", snapshot)
        quantities = snapshot.get("quantities")
        weights = snapshot.get("account_weights") or snapshot.get("weights", {})
        expected = (1 if self.config.side == "long" else -1) * target
        if weights.get(self.config.symbol) != expected:
            raise Conditions("账户杠杆与精度处理后的目标不符合约定")
        if quantities is not None and quantities.get(self.config.symbol) != expected:
            raise Conditions("目标换算后可执行手数不符合约定")

    def trigger(self, step, budget=2):
        """一次触发后立即存 execution_id；断连留下提交意图，不自动再触发。"""
        if self.requests + budget > self.config.max_requests:
            raise Conditions("已达到批次 max_requests，停止推进")
        self.requests += budget  # 每笔可能的报单预留一次风险撤单，今昨拆单分别预留。
        previous = self.active_execution
        self.active_execution = "submission-unknown"
        try:
            receipt = self.call("POST", f"/account/execute/{self.account_id}")
        except ApiError as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                self.active_execution = previous
            raise
        self.active_execution = receipt["execution_id"]
        self.execution_ids.append(self.active_execution)
        self.pending_execution_ids.add(self.active_execution)
        self.bundle.write(f"{step}/receipt.json", receipt)
        self.bundle.index["execution_ids"] = self.execution_ids
        self.bundle.write("index.json", self.bundle.index)
        return self.active_execution

    def wait(self, execution_id, step, extra_seconds=0):
        deadline = self.clock.time() + self.config.timeout_seconds + extra_seconds + 20
        while self.clock.time() < deadline:
            state = self.call("GET", f"/account/executions/{execution_id}")
            self.bundle.write(f"{step}/status.json", state)
            if state["status"] in TERMINAL:
                self.pending_execution_ids.discard(execution_id)
                if self.active_execution == execution_id:
                    self.active_execution = None
                return state
            self.clock.sleep(self.config.poll_seconds)
        self.bundle.write(f"{step}/timeout.json", {"status": "unknown", "execution_id": execution_id})
        # graceful 保留算法自己的逐笔撤单与回报，不使用账户级批量撤单。
        self.call("POST", self.base + "/terminate", {"mode": "graceful", "reason": "FT 场景等待超时"})
        raise TimeoutError("已请求终止但尚未确认实际结束；保留未知状态，禁止自动重跑")

    def collect(self, execution_id, step):
        """ZIP 优先；逐接口查询用于判定与核对必需材料。"""
        state = self.api.request("GET", f"/account/executions/{execution_id}")
        if state.get("account_id") != self.account_id:
            raise Conditions("execution 不属于所选账户，拒绝采集")
        if state.get("status") not in TERMINAL:
            raise Conditions("execution 尚未结束，拒绝采集不稳定证据")
        self.bundle.write(f"{step}/status.json", state)
        collected = {}
        for suffix in ("events", "artifacts", "control/events"):
            try:
                page = self.api.pages(f"/account/executions/{execution_id}/{suffix}")
                self.bundle.write(f"{step}/{suffix.replace('/', '-')}.json", page)
                collected[suffix] = page
                if not page.get("complete"):
                    self.bundle.index["missing"].append(f"{step}: {suffix} 分页不完整")
            except Exception as exc:
                collected[suffix] = {"data": [], "complete": False}
                self.bundle.index["missing"].append(f"{step}: {suffix}: {exc}")
        try:
            data = self.call(
                "GET", f"/account/executions/{execution_id}/evidence?profile=shareable&include_logs=true", binary=True
            )
            self.save_zip(f"{step}/product-evidence.zip", data)
        except Exception as exc:
            self.bundle.index["missing"].append(f"{step}: 产品 ZIP 导出缺失: {exc}")
        return collected

    def save_zip(self, name, data):
        """验证 ZIP 完整性并拒绝含已知明文秘密的分享副本。"""
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if archive.testzip():
                raise ValueError("证据 ZIP 校验失败")
            for member in archive.infolist():
                text = archive.read(member).decode(errors="replace")
                if any(secret in text for secret in self.bundle.redact.secrets if len(secret) >= 6):
                    raise ValueError("产品分享 ZIP 仍含已知敏感值，拒绝保存")
            manifest = json.loads(archive.read("manifest.json"))
            for item in manifest.get("files", []):
                payload = archive.read(item["path"])
                if hashlib.sha256(payload).hexdigest() != item["sha256"] or len(payload) != item["bytes"]:
                    raise ValueError("产品证据 manifest 文件校验失败")
            self.coverage(name, manifest)
        path = self.bundle.path / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)

    def coverage(self, name, manifest):
        """沿用产品材料覆盖报告，只把本批次必需材料缺口加入索引。"""
        self.bundle.write(name.removesuffix(".zip") + "-coverage.json", manifest)
        self.bundle.index["product_exporter"] = manifest.get("exporter")
        logs = manifest.get("logs", {})
        if not logs.get("read_complete"):
            self.bundle.index["missing"].append(f"{name}: 运行日志不完整 {logs.get('issues', [])}")
        for row in manifest.get("executions", []):
            if row.get("execution_id") not in self.execution_ids:
                continue
            coverage = row.get("coverage", {})
            if coverage.get("coverage") != "available":
                self.bundle.index["missing"].append(f"{name}: {row.get('execution_id')} 材料覆盖: {coverage}")

    def trade(self, action, step, **overrides):
        self.configure(action, step, **overrides)
        before = self.call("GET", self.base + "/control/monitor")
        budget = 2 * min(2, self.config.lots) if action == "close" else 2
        execution_id = self.trigger(step, budget)
        self.bundle.write(f"{step}/monitor-running.json", self.call("GET", self.base + "/control/monitor"))
        try:
            self.wait(execution_id, step)
        finally:
            collected = self.collect(execution_id, step)
        after = self.call("GET", self.base + "/control/monitor")
        observed = self.call("POST", self.base + "/assets/refresh")
        self.bundle.write(f"{step}/observed-assets.json", observed)
        self.check_preserved(observed["assets"])
        accounting = reconcile(before, after, collected["control/events"])
        self.bundle.write(f"{step}/reconciliation.json", accounting)
        self.write_table(step, accounting["rows"])
        results = [r for r in find_results(collected["artifacts"]) if r.get("symbol") == self.config.symbol]
        if not results:
            core = [r for r in find_core_results(collected["artifacts"]) if r.get("symbol") == self.config.symbol]
            pending = self.call("GET", self.base + "/orders?refresh=true")
            self.bundle.write(f"{step}/observed-orders.json", pending)
            if core:
                status, reason = judge_core_noop(action, core[-1], self.config, observed["assets"], pending, accounting)
            else:
                failures = [
                    e.get("details", {})
                    for e in collected["events"].get("data", [])
                    if e.get("event_type") == "execution_failed"
                ]
                detail = failures[-1] if failures else {}
                raw = str(detail.get("technical_detail") or detail.get("error") or "缺少场景算法结果")
                status = "insufficient_conditions" if "行情等待超时" in raw else "unknown"
                reason = "未进入场景算法：" + raw[:240]
        else:
            status, reason = judge(action, results[-1], self.config)
        return {
            "scenario": step,
            "status": status,
            "reason": reason,
            "execution_id": execution_id,
            "accounting_complete": accounting["complete"],
            "evidence": step,
            "expected_action": action,
        }

    def write_table(self, step, rows):
        columns = ("event_uid", "operation", "symbol", "decision", "counted", "outcome", "occurred_at_ms", "metadata")
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in self.bundle.redact(rows):
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
                    for key, value in row.items()
                }
            )
        path = self.bundle.path / step / "逐笔对账.csv"
        path.write_text(stream.getvalue(), encoding="utf-8-sig")
        path.chmod(0o600)

    def stats(self, step):
        rows = []
        for number in range(self.config.repetitions):
            row = self.trade("cancel", f"{step}/{number + 1:02d}")
            rows.append(row)
            if row["status"] != "passed":
                break
        self.bundle.write(f"{step}/iterations.json", rows)
        good = len(rows) == self.config.repetitions and all(
            r["status"] == "passed" and r["accounting_complete"] for r in rows
        )
        return {
            "scenario": step,
            "status": "passed" if good else "unknown",
            "reason": "逐笔报撤及本地计数一致" if good else "部分操作或对账不完整，停止后续重复操作",
        }

    def restore(self):
        """仅在实际结束后恢复配置；未知提交不覆盖仍在使用的算法。"""
        if not self.original:
            return
        if self.active_execution or self.pending_execution_ids:
            self.bundle.index["missing"].append("执行尚未确认结束，配置未恢复；按 restore.json 人工核对")
            return
        try:
            self.call("PATCH", self.base, self.original)
            self.bundle.write("restored.json", {"at": clock_now_iso(), "restored": True})
        except Exception as exc:
            self.bundle.index["missing"].append(f"账户配置恢复失败: {exc}")

    def stop_owned(self):
        """异常退出也不能留下等待中的测试任务日后继续发单。"""
        if not self.pending_execution_ids:
            return
        try:
            activity = self.activity()
            active = {activity.get("running_execution_id"), activity.get("pending_execution_id")}
            if active & self.pending_execution_ids:
                try:
                    self.call("POST", self.base + "/terminate", {"mode": "graceful", "reason": "FT 场景异常收尾"})
                except ApiError as exc:
                    if exc.status != 409:
                        raise
                    # 任务可能在读取槽位后自然结束；逐 ID 核对终态，409 本身不算结束证明。
            for execution_id in list(self.pending_execution_ids):
                self.wait(execution_id, "cleanup/" + execution_id)
                self.collect(execution_id, "cleanup/" + execution_id)
        except Exception as exc:
            self.bundle.index["missing"].append(f"测试任务收尾未确认: {exc}")
