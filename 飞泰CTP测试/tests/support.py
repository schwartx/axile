"""离线柜台替身：运行同一个场景算法，绝不导入或连接 CTP SDK。"""

import copy
import hashlib
import io
import json
import math
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from axile.executor.algorithms.core.base import AlgorithmInput
from axile.executor.algorithms.utils.clock import clock_now_iso, get_default_clock
from axile.executor.constants.order_status import OrderStatus
from axile.executor.futures_order_intent import plan_futures_close_orders
from axile.executor.models.unified_account_assets import Position, UnifiedAccountAssets
from axile.executor.models.unified_order import OrderDirection, OrderType, TradeRecord, UnifiedOrder
from axile.executor.models.unified_price import UnifiedPriceData
from axile.executor.order_volume_limits import ensure_order_volume_allowed

from ft_ctp.algorithm import ScenarioParams, scenario_algorithm
from ft_ctp.api import Api, ApiError
from ft_ctp.config import Config


@dataclass
class HarnessConfig(Config):
    offline_fault: str = "none"
    offline_initial_lots: int = 0
    offline_yesterday_lots: int = 0


class OfflineClock:
    def __init__(self):
        self.now = 1792108800.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def event_wait(self, event, timeout):
        if event.is_set():
            return True
        self.sleep(timeout)
        return event.is_set()


class OfflineSession:
    def __init__(self, api, execution_id, params):
        self.api, self.execution_id, self.params = api, execution_id, params
        self.symbol = params.symbol
        self.order_callbacks, self.trade_callbacks = [], []
        self.orders, self.trades = {}, {}
        self.terminated = False
        self.native_calls = 0

    def get_account_assets(self):
        return self.api.assets()

    def get_current_volume(self, assets):
        return self.api.volume

    def get_pending_orders(self):
        if self.api.config.offline_fault == "unknown":
            raise TimeoutError("离线注入：挂单读取未知")
        return [o for o in self.orders.values() if not o.is_completed()]

    def get_market_data(self):
        return UnifiedPriceData(
            symbol=self.symbol,
            last_price=3500,
            bid_price=3499,
            ask_price=3501,
            bid_volume=10,
            ask_volume=10,
            volume=100,
            timestamp=get_default_clock().time() * 1000,
            update_time=clock_now_iso(),
            extra={"lower_limit_price": 3200, "upper_limit_price": 3800},
        )

    def get_tick_size(self):
        return 1.0

    def plan_close_orders(self, direction, volume, assets):
        return plan_futures_close_orders(self.symbol, direction, volume, assets, "SHFE")

    def register_order_callback(self, callback):
        self.order_callbacks.append(callback)

    def unregister_order_callback(self, callback):
        self.order_callbacks.remove(callback)

    def register_trade_callback(self, callback):
        self.trade_callbacks.append(callback)

    def unregister_trade_callback(self, callback):
        self.trade_callbacks.remove(callback)

    def handle_termination_checkpoint(self):
        if self.terminated:
            raise RuntimeError("离线注入：已终止")

    def sleep_or_terminate(self, seconds):
        self.handle_termination_checkpoint()
        get_default_clock().sleep(seconds)

    def emit_audit_event(self, **event):
        event.update(id=len(self.api.events[self.execution_id]) + 1)
        self.api.events[self.execution_id].append(event)
        return True

    def publish(self, order):
        self.orders[order.order_id] = order.model_copy(deep=True)
        for callback in self.order_callbacks:
            callback(order)

    def fill(self, order, quantity):
        if not quantity:
            return
        sign = 1 if order.direction == "BUY" else -1
        self.api.volume += sign * quantity
        if order.extra.get("offset_flag") == "close_yesterday":
            self.api.yesterday_lots = max(0, self.api.yesterday_lots - quantity)
        trade = TradeRecord.create(
            trade_id="offline-trade-" + order.order_id,
            symbol=self.symbol,
            order_id=order.order_id,
            trade_volume=quantity,
            trade_price=order.price,
        )
        self.trades[trade.trade_id] = trade
        for callback in self.trade_callbacks:
            callback(trade)

    def place_order(self, direction, order_type, volume, price, **kwargs):
        self.api.control(self.execution_id, "place_order")
        ensure_order_volume_allowed(
            volume,
            instrument={"MinLimitOrderVolume": 1, "MaxLimitOrderVolume": 100},
            order_type=order_type,
            trade_rule=kwargs.get("trade_rule"),
        )
        if not price.is_integer() or not 3200 <= price <= 3800:
            raise ValueError(f"限价 {price} 不符合 tick 或涨跌停")
        if self.api.config.offline_fault == "sync_reject":
            error = RuntimeError("离线注入：同步拒绝 return_code=-1")
            error.return_code = -1
            raise error
        self.native_calls += 1
        identity = f"offline-{self.api.account['id']}-{self.execution_id}-{self.native_calls}"
        passive = self.params.action in {"cancel", "hold"}
        fault = self.api.config.offline_fault
        filled = volume if not passive else 0
        status = OrderStatus.PENDING if passive else OrderStatus.FILLED
        extra = {
            "order_ref": str(self.native_calls),
            "front_id": 1,
            "session_id": 1,
            "order_sys_id": identity,
            "exchange_id": "SHFE",
            **kwargs,
        }
        if self.params.action == "reject":
            status, filled = OrderStatus.REJECTED, 0
            extra.update(ErrorID=self.api.config.expected_error_id or 90001, ErrorMsg="离线模拟拒绝，非柜台证据")
        elif fault == "partial":
            filled, status = max(1, int(volume) // 2), OrderStatus.PARTIALLY_FILLED
        elif fault == "timeout":
            filled, status = 0, OrderStatus.PENDING
            extra["order_sys_id"] = ""
        order = UnifiedOrder.create(
            order_id=identity,
            symbol=self.symbol,
            direction=direction.value,
            order_type=order_type.value,
            volume=volume,
            price=price,
            status=status,
            filled_volume=filled,
            **extra,
        )
        # 刻意同步交付回报，覆盖回报早于 place_order 返回的竞争。
        self.fill(order, filled)
        self.publish(order)
        if fault == "late":
            late = order.model_copy(update={"status": OrderStatus.PENDING, "filled_volume": 0})
            for callback in self.order_callbacks:
                callback(late)
        return order.model_copy(update={"status": OrderStatus.SUBMITTED, "filled_volume": 0})

    def cancel_order(self, order_id):
        self.api.control(self.execution_id, "cancel_order")
        self.api.control(self.execution_id, "cancel_order_ctp")
        if self.api.config.offline_fault == "cancel_failed":
            raise RuntimeError("离线注入：撤单请求被拒绝")
        order = self.orders[order_id].model_copy(update={"status": OrderStatus.CANCELED})
        self.publish(order)
        return True

    def query_trades(self, order_id):
        return [t for t in self.trades.values() if t.order_id == order_id]


class OfflineApi(Api):
    """用 API 契约驱动真实脚本代码，结果清楚标记为离线。"""

    def __init__(self, config):
        if not isinstance(config, HarnessConfig):
            config = HarnessConfig(**asdict(config))
        self.config = config
        self.volume = config.offline_initial_lots
        self.yesterday_lots = config.offline_yesterday_lots
        self.account = {
            "id": 4,
            "name": config.account_name,
            "trade_channel": "ctp",
            "is_started": False,
            "algorithm": {"method": "TARGET-POS-TASK"},
            "trade_rules": {},
            "portfolio_id": 4,
            "long_leverage": 1,
            "short_leverage": 1,
            "weight_precision": 0.01,
            "cron_expr": "",
            "account_control_override": None,
            "supplement": None,
            "execution_timeout": 60,
        }
        self.portfolio, self.executions, self.events, self.artifacts, self.controls = {}, {}, {}, {}, {}
        self.sessions = {}
        self.extra_positions = []
        self.counts = {"place_order": 0, "cancel_order": 0, "cancel_order_ctp": 0}
        self.target = config.lots
        self.instance_id = "offline-instance"
        self.scheduled_at = None
        self.clock = get_default_clock()

    def assets(self):
        positions = list(self.extra_positions)
        if self.volume:
            prefix = "long" if self.volume > 0 else "short"
            positions.append(
                Position(
                    symbol=self.config.symbol,
                    volume=abs(self.volume),
                    available_volume=abs(self.volume),
                    market_value=abs(self.volume) * 3500,
                    direction="多头" if self.volume > 0 else "空头",
                    extra={
                        f"{prefix}_td": abs(self.volume) - self.yesterday_lots,
                        f"{prefix}_yd": self.yesterday_lots,
                    },
                )
            )
        return UnifiedAccountAssets(available_cash=100000, total_asset=100000, market_value=0, positions=positions)

    def control(self, execution_id, operation):
        self.counts[operation] += 1
        metadata = {}
        override = self.account.get("account_control_override") or {}
        policy = override.get("operations", {}).get(operation, {})
        percent = policy.get("daily_warning_percent")
        limit = policy.get("account", {}).get("per_day", {}).get("limit")
        if percent and limit and self.counts[operation] == math.ceil(percent * limit / 100):
            metadata["quota_warning"] = {"source": "offline_axile_contract", "used": self.counts[operation]}
        rows = self.controls[execution_id]
        rows.append(
            {
                "event_uid": f"{execution_id}-{len(rows)}",
                "operation": operation,
                "symbol": self.config.symbol,
                "decision": "allowed",
                "counted": True,
                "outcome": "submitted",
                "metadata": metadata,
                "occurred_at_ms": int(self.clock.time() * 1000),
            }
        )

    def monitor(self):
        active = [
            key for key, row in self.executions.items() if row["status"] not in {"SUCCEEDED", "FAILED", "TERMINATED"}
        ]
        waiting = (
            [
                {
                    "kind": "quota",
                    "hits": [
                        {"operation": "place_order", "rule_kind": "per_minute", "scope_type": "account", "limit": 0}
                    ],
                }
            ]
            if active and self.config.pause_point == "quota"
            else []
        )
        return {
            "account_id": 4,
            "instance_id": self.instance_id,
            "control_date": "2026-10-16",
            "timezone": "Asia/Shanghai",
            "stale": False,
            "incomplete": False,
            "pending_policy": False,
            "execution_id": active[-1] if active else None,
            "waiting": waiting,
            "rules": [],
            "metrics": [
                {"operation": k, "scope": "account", "rule": "per_day", "value": v} for k, v in self.counts.items()
            ],
        }

    def execute(self):
        execution_id = f"offline-{len(self.executions) + 1:04d}"
        params = ScenarioParams.model_validate(self.account["algorithm"]["params"])
        self.events[execution_id], self.controls[execution_id] = [], []
        session = OfflineSession(self, execution_id, params)
        self.sessions[execution_id] = session
        self.executions[execution_id] = {"execution_id": execution_id, "account_id": 4, "status": "RUNNING"}
        if params.action == "hold":
            checkpoint = {
                "reason_code": "FT.CHECKPOINT.ready",
                "details": {"checkpoint": "ready", "batch": params.batch, "step": params.step},
            }
            self.events[execution_id].append(checkpoint)
            if self.config.pause_point == "executing" and self.config.pause_checkpoint != "ready":
                stage = self.config.pause_checkpoint
                if stage == "after_send":
                    direction = OrderDirection.BUY if params.side == "long" else OrderDirection.SELL
                    session.place_order(
                        direction,
                        OrderType.LIMIT,
                        params.lots,
                        3490.0 if params.side == "long" else 3510.0,
                        offset_flag="open",
                        trade_rule={"max_single_order_size": params.max_order_lots},
                    )
                self.events[execution_id].append(
                    {
                        "reason_code": f"FT.CHECKPOINT.{stage}",
                        "details": {"checkpoint": stage, "batch": params.batch, "step": params.step},
                    }
                )
            if self.config.pause_point == "queued" and len(self.sessions) > 1:
                self.executions[execution_id]["status"] = "QUEUED"
        else:
            result = scenario_algorithm(
                session,
                AlgorithmInput(
                    symbol=params.symbol, target_volume=self.target, trade_rule={"sizing_mode": "lots"}, params=params
                ),
            )
            self.artifacts[execution_id] = [
                {
                    "id": 1,
                    "artifact_type": "execution_summary",
                    "content": {"results": [result.model_dump(mode="json")]},
                }
            ]
            self.executions[execution_id]["status"] = "SUCCEEDED" if result.success else "FAILED"
        return {"execution_id": execution_id, "accepted": "created", "account_id": 4}

    def request(self, method, path, body=None, binary=False):
        route = path.split("?")[0]
        simple = {
            "/account/dashboard": {"data": [{"account_id": 4, "running_execution_id": self.monitor()["execution_id"]}]},
            "/utils/health-check/": True,
            "/algorithms": [{"name": "FT-CTP-SCENARIO"}],
            "/account/4/connection": {"status": "ready", "worker_alive": True},
            "/account/4/connection/reconnect": {"status": "ready", "worker_alive": True},
        }
        if route in simple:
            return simple[route]
        if route == "/instrument-catalog/query":
            raise ApiError("CATALOG.NOT_FOUND: 合约查询返回空结果或合约已下市", 404)
        if binary:
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                payload = json.dumps({"mode": "offline", "executions": self.executions}).encode()
                archive.writestr("offline-evidence.json", payload)
                archive.writestr(
                    "manifest.json",
                    json.dumps(
                        {
                            "schema_version": 2,
                            "exporter": {"mode": "offline"},
                            "files": [
                                {
                                    "path": "offline-evidence.json",
                                    "sha256": hashlib.sha256(payload).hexdigest(),
                                    "bytes": len(payload),
                                }
                            ],
                            "logs": {"read_complete": True, "source": "offline"},
                            "executions": [],
                        }
                    ),
                )
            return stream.getvalue()
        if route == "/portfolio/" and method == "POST":
            self.portfolio = {**body, "id": 99}
            return self.portfolio
        if route == "/portfolio/99":
            return self.update_portfolio(body)
        if route == "/account/4":
            return self.account_resource(method, body)
        if route.endswith("/orders"):
            orders = [
                o.model_dump(mode="json")
                for s in self.sessions.values()
                for o in s.orders.values()
                if not o.is_completed()
            ]
            return {"data": orders, "complete": True, "count": len(orders)}
        if route.endswith("/assets/refresh"):
            return {"assets": self.assets().model_dump(mode="json"), "account_id": 4}
        if route.endswith("/control/monitor"):
            return self.monitor()
        if route.endswith("/target_snapshot/refresh"):
            return self.target_snapshot()
        if route.endswith("/next_run_time"):
            stamp = datetime.fromtimestamp(self.scheduled_at or self.clock.time(), timezone.utc).isoformat()
            return {"is_scheduled": self.account["is_started"], "next_execution_times": [stamp]}
        if route == "/account/execute/4":
            return self.execute()
        if route.endswith("/terminate"):
            return self.terminate()
        if route.startswith("/account/executions/"):
            return self.execution_resource(path, route)
        raise ApiError(f"离线 API 未实现 {method} {path}", 404)

    def update_portfolio(self, body):
        """只回放脚本自己生成的固定目标函数，不加载外部组合。"""
        self.portfolio.update(body)
        code = body.get("custom_calc_py_code", "")
        if "context.get_quote" not in code and code:
            import ast

            self.target = ast.literal_eval(code.split("return ", 1)[1].strip())[self.config.symbol]
        return copy.deepcopy(self.portfolio)

    def target_snapshot(self):
        if "context.get_quote" in self.portfolio.get("custom_calc_py_code", ""):
            raise ApiError(f"未知 CTP 合约: {self.config.unknown_symbol}", 422)
        return {"weights": {self.config.symbol: self.target}, "quantities": {self.config.symbol: self.target}}

    def account_resource(self, method, body):
        if method == "PATCH":
            self.account.update(copy.deepcopy(body))
            if body.get("is_started"):
                self.scheduled_at = self.clock.time() + 60
        if self.scheduled_at and self.account["is_started"] and self.clock.time() >= self.scheduled_at:
            self.account["has_pending_supplement"] = bool(self.account.get("supplement"))
            self.execute()
            self.scheduled_at = None if self.config.pause_point == "supplement" else self.clock.time() + 60
        return copy.deepcopy(self.account)

    def terminate(self):
        active = [r for r in self.executions.values() if r["status"] in {"RUNNING", "QUEUED"}]
        self.account["has_pending_supplement"] = False
        if not active and self.config.pause_point == "idle":
            raise ApiError("账户当前无活跃执行", 409)
        for row in active:
            session = self.sessions[row["execution_id"]]
            for order in list(session.orders.values()):
                if not order.is_completed():
                    session.cancel_order(order.order_id)
            row["status"] = "TERMINATED"
            self.events[row["execution_id"]].append({"reason_code": "FT.OFFLINE.TERMINATED"})
            self.artifacts[row["execution_id"]] = []
        return {"status": "TERMINATING", "execution_id": active[-1]["execution_id"] if active else None}

    def execution_resource(self, path, route):
        from urllib.parse import parse_qs

        key = route.split("/")[3]
        if route.endswith("/control/events"):
            rows = self.controls[key]
        elif route.endswith("/artifacts"):
            rows = self.artifacts.get(key, [])
        elif route.endswith("/events"):
            rows = self.events[key]
        else:
            state = self.executions[key]
            if state["status"] == "RUNNING" and self.config.pause_action == "pause_auto":
                state["status"] = "SUCCEEDED"
                self.artifacts[key] = []
            return copy.deepcopy(state)
        query = parse_qs(path.partition("?")[2])
        skip, limit = int(query.get("skip", [0])[0]), int(query.get("limit", [200])[0])
        return {"data": copy.deepcopy(rows[skip : skip + limit]), "count": len(rows)}
