"""冷启动算法发现和 HTTP 校验必须独立于可选 OpenCTP SDK。"""

import subprocess
import sys
from pathlib import Path

import pytest

_BOOTSTRAP = """
import importlib.abc
import importlib.metadata
import sys

attempts = []
class BlockOpenCTP(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "openctp_ctp" or fullname.startswith("openctp_ctp."):
            attempts.append(fullname)
            raise ModuleNotFoundError(f"Blocked optional SDK: {fullname}", name=fullname)

assert not any(name.startswith("openctp_ctp") for name in sys.modules)
sys.meta_path.insert(0, BlockOpenCTP())
# 只隔离外部算法插件，保留真实内置算法加载器和其他 entry points。
original_entry_points = importlib.metadata.entry_points
def builtin_only(**kwargs):
    entries = importlib.metadata.EntryPoints(
        ep for ep in original_entry_points() if ep.group != "axile.algorithms"
    )
    return entries.select(**kwargs)
importlib.metadata.entry_points = builtin_only
"""

_METADATA = """
import json
from axile.executor.algorithms.core.base import (
    get_algorithm, get_algorithm_metadata, list_algorithms_metadata,
)
first = list_algorithms_metadata()
assert first == list_algorithms_metadata()
by_name = {meta.name: meta for meta in first}
assert len(first) == len(by_name)
assert {"SINGLE-MAKER", "TWAP", "POV", "TARGET-POS-TASK", "CTP_OPTION_EXERCISE"} <= by_name.keys()
assert by_name["CTP_OPTION_EXERCISE"].channels == frozenset({"ctp"})
assert by_name["CTP_OPTION_EXERCISE"].slots == frozenset()
assert callable(get_algorithm("SINGLE-MAKER"))
schema = get_algorithm_metadata("SINGLE-MAKER").params_schema
assert json.loads(json.dumps(schema))["type"] == "object"
"""

_HTTP_SETUP = """
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from axile.server.api.routes import account_crud, algorithms
from axile.server.execution import notification_outbox
from tests.unit.server.test_account_control_routes import (
    _account_payload, _build_app, _noop_async, _RouteSession,
    _seed_account_control_registry, _synchronized_runtime_sync,
)
_seed_account_control_registry.__wrapped__()
patch = MonkeyPatch()
# 路由仅验证算法发现与账户契约，通知出站任务不能访问真实数据库。
patch.setattr(notification_outbox, "wake_notifications", lambda account_id: None)
patch.setattr(account_crud, "parse_cron_expr", lambda expr: ["fake-trigger"])
patch.setattr(account_crud, "add_record_portfolio_account", _noop_async)
patch.setattr(account_crud, "enqueue_account_runtime_sync", _noop_async)
patch.setattr(account_crud, "reconcile_account_runtime", _synchronized_runtime_sync)
session = _RouteSession()
app = _build_app(session)
app.include_router(algorithms.router)
client = TestClient(app)
"""

_HTTP_LIST = """
response = client.get("/algorithms")
assert response.status_code == 200, response.text
items = [algorithms.AlgorithmPublic.model_validate(item) for item in response.json()]
assert {item.name for item in items} >= {"SINGLE-MAKER", "CTP_OPTION_EXERCISE"}
assert client.get("/algorithms").json() == response.json()
"""

_HTTP_ACCOUNT = """
payload = _account_payload()
payload.update(
    name="tq-test-sim", trade_channel="tq", brokerage="tq", is_started=False,
    portfolio_id=None, account_control_override=None,
    account_config={"account_mode": "sim", "tq_username": "test", "tq_password": "test"},
)
response = client.post("/account/", json=payload)
assert response.status_code == 201, response.text
assert session.account.trade_channel == "tq"
assert session.account.algorithm["method"] == "SINGLE-MAKER"
assert response.json()["trade_channel"] == "tq"
updated = {"method": "TWAP", "params": {"slices": 5}}
response = client.patch("/account/1", json={"algorithm": updated})
assert response.status_code == 200, response.text
assert session.account.algorithm == updated
assert response.json()["algorithm"] == updated
for invalid, expected in [
    ({"params": {}}, "method"),
    ({"method": "SINGLE-MAKER", "params": {"max_wait_seconds": 0}}, "参数不合法"),
]:
    response = client.post("/account/", json={**payload, "algorithm": invalid})
    assert response.status_code == 422, response.text
    assert expected in response.text
    response = client.patch("/account/1", json={"algorithm": invalid})
    assert response.status_code == 422, response.text
    assert expected in response.text
assert session.account.algorithm == updated
# 未知插件算法按既有契约允许保存，由执行期负责解析。
unknown = {"method": "DOES-NOT-EXIST", "params": {}}
response = client.post("/account/", json={**payload, "algorithm": unknown})
assert response.status_code == 201, response.text
response = client.patch("/account/1", json={"algorithm": unknown})
assert response.status_code == 200, response.text
assert session.account.algorithm == unknown
"""


@pytest.mark.parametrize(
    "scenario",
    [
        "import axile.executor.algorithms.defaults.ctp_option_exercise.impl",
        _METADATA,
        _HTTP_SETUP + _HTTP_LIST,
        _HTTP_SETUP + _HTTP_ACCOUNT,
    ],
    ids=["direct-import", "metadata", "http-list", "http-tq-account"],
)
def test_builtin_algorithms_without_openctp(scenario: str) -> None:
    # 每个入口独立冷启动，防止注册表缓存掩盖首次加载失败。
    result = subprocess.run(
        [sys.executable, "-c", _BOOTSTRAP + scenario + "\nassert attempts == [], attempts\n"],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Child exit code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
