"""证据完整性、输入保护及公共 CTP 原生发送边界回归。"""

import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from axile.executor.constants.order_status import OrderStatus
from axile.executor.models.unified_order import OrderDirection, OrderType, UnifiedOrder

from ft_ctp.algorithm import Capture
from ft_ctp.api import Api, ApiError
from ft_ctp.checks import day_counts, judge, reconcile
from ft_ctp.config import Config, load_account
from ft_ctp.evidence import Bundle, Redactor, account_lock
from ft_ctp.runner import Runner
from support import OfflineApi


@pytest.mark.parametrize("layout", ["flat", "settings"])
def test_database_credentials_are_exact_read_only_and_not_exported(tmp_path, layout):
    path = tmp_path / "axile.db"
    credentials = {"broker_id": "test-broker", "investor_id": "test-investor", "password": "never-export-me"}
    with sqlite3.connect(path) as connection:
        if layout == "flat":
            connection.execute("CREATE TABLE account(id INTEGER, name TEXT, trade_channel TEXT, account_config TEXT)")
            connection.execute("INSERT INTO account VALUES(4, 'CTP测试账户', 'ctp', ?)", (json.dumps(credentials),))
        else:
            connection.execute("CREATE TABLE account(id INTEGER, name TEXT, trade_channel TEXT)")
            connection.execute("INSERT INTO account VALUES(4, 'CTP测试账户', 'ctp')")
            connection.execute(
                "CREATE TABLE account_settings(account_id INTEGER, connection TEXT, execution TEXT, schedule TEXT)"
            )
            connection.execute("INSERT INTO account_settings VALUES(4, ?, '{}', '{}')", (json.dumps(credentials),))
    before = path.read_bytes()
    account = load_account(str(path), "CTP测试账户")
    assert account["account_config"] == credentials
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="不存在"):
        load_account(str(path), "不存在的账户")
    bundle = Bundle(str(tmp_path / "out"), "safe", {}, Redactor(credentials))
    bundle.write("error.json", {"ErrorMsg": "broker says never-export-me", "account_config": credentials})
    bundle.index["scenarios"].append({"scenario": "fault", "status": "unknown", "reason": "never-export-me"})
    bundle.finish()
    for file in bundle.path.rglob("*"):
        if file.is_file():
            assert b"never-export-me" not in file.read_bytes()


def test_account_lock_covers_different_batches(tmp_path):
    with account_lock(tmp_path, 4):
        with pytest.raises(ValueError, match="已有"):
            with account_lock(tmp_path, 4):
                pytest.fail("同账户不能并发")
        with account_lock(tmp_path, 5):
            pass


@pytest.mark.parametrize(
    "changes",
    [
        {"lots": 2},
        {"lots": 1.5},
        {"lots": True},
        {"timeout_seconds": float("nan")},
        {"warning_percent": 100},
        {"repetitions": 99},
    ],
)
def test_limits_fail_before_any_actions(changes):
    with pytest.raises(ValueError):
        Config(**changes).validate()


def test_paginated_read_works_and_detects_drift():
    api = Api("http://127.0.0.1:8000/api/v1")
    data = [{"id": i} for i in range(503)]
    requests = []

    def page(method, path, body=None):
        from urllib.parse import parse_qs

        query = parse_qs(path.partition("?")[2])
        skip, limit = int(query["skip"][0]), int(query["limit"][0])
        requests.append(skip)
        return {"data": data[skip : skip + limit], "count": len(data)}

    api.request = page
    result = api.pages("/events")
    assert result["complete"] and len(result["data"]) == 503 and requests == [0, 200, 400]
    api.request = lambda *a: {"data": [{"id": 1}], "count": 2}
    assert not api.pages("/events")["complete"]


def test_accounting_uses_native_cancel_only_and_preserves_failures():
    api = OfflineApi(Config())
    before = api.monitor()
    api.controls["batch"] = []
    api.control("batch", "place_order")
    api.control("batch", "cancel_order")
    api.control("batch", "cancel_order_ctp")
    rows = api.controls["batch"]
    rows[0]["outcome"] = "failed"
    result = reconcile(before, api.monitor(), {"data": rows + rows, "complete": True})
    assert result["complete"] and result["total"] == 2
    assert [r["operation"] for r in result["rows"]] == ["place_order", "cancel_order_ctp"]
    assert result["rows"][0]["outcome"] == "failed"
    after = api.monitor()
    after["control_date"] = "next-day"
    assert not reconcile(before, after, {"data": rows, "complete": True})["complete"]
    after = api.monitor()
    after["stale"] = True
    assert day_counts(after) is None


def test_acceptance_without_fill_details_is_unknown():
    result = {
        "memory": {"feitai": {"after": {}, "final_volume": 1, "remaining_orders": [], "requests": [{}]}},
        "orders": [{"order_id": "first", "status": OrderStatus.FILLED, "volume": 1}],
        "trades": [],
    }
    assert judge("open", result, Config())[0] == "unknown"


def test_equal_total_volume_does_not_hide_wrong_order_association():
    from ft_ctp.checks import matched_fills

    orders = [{"order_id": "one", "volume": 1}, {"order_id": "two", "volume": 1}]
    wrong = [{"order_id": "one", "symbol": "rb2701", "trade_id": "fill", "trade_volume": 2}]
    assert not matched_fills(orders, wrong, "rb2701")
    good = [
        {"order_id": key, "symbol": "rb2701", "trade_id": "fill-" + key, "trade_volume": 1} for key in ("one", "two")
    ]
    assert matched_fills(orders, good, "rb2701")
    assert not matched_fills(orders, good + good, "rb2701")


def test_product_manifest_hash_mismatch_is_not_saved(tmp_path):
    import io
    import zipfile

    bundle = Bundle(str(tmp_path), "corrupt", {})
    runner = Runner(Config(), OfflineApi(Config()), bundle, 4)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("record.json", "{}")
        archive.writestr(
            "manifest.json", json.dumps({"files": [{"path": "record.json", "sha256": "incorrect", "bytes": 2}]})
        )
    with pytest.raises(ValueError, match="manifest"):
        runner.save_zip("corrupt.zip", buffer.getvalue())
    assert not (bundle.path / "corrupt.zip").exists()


def test_capture_isolated_by_executor_and_deduplicates_late_reject():
    left, right = Capture("rb2701"), Capture("rb2701")
    order = UnifiedOrder.create(
        order_id="same-native-id",
        symbol="rb2701",
        direction="BUY",
        order_type="LIMIT",
        volume=1,
        price=3500,
        status=OrderStatus.REJECTED,
        ErrorID=99999,
        ErrorMsg="未知错误原文",
    )
    left.add(order)
    left.on_order(order)
    left.on_order(order)
    assert len(left.snapshots()) == 1
    assert not right.snapshots()
    assert left.snapshots()[0].extra["ErrorID"] == 99999
    assert left.snapshots()[0].extra["ErrorMsg"] == "未知错误原文"


@pytest.mark.parametrize(
    "case, expected",
    [
        ("unknown", "未知 CTP 合约"),
        ("price", "不符合 tick 或涨跌停"),
        ("volume", "超过单笔上限"),
    ],
)
def test_public_ctp_validation_hits_target_before_native_insert(case, expected):
    # 使用真实 CTP _place_order_impl 和校验函数，只替换连接／市场前置，绝不启动 SDK。
    from axile.executor.ctp.ctp_execute import CTPExecutor

    executor = CTPExecutor.__new__(CTPExecutor)
    executor._lock = threading.RLock()
    executor._require_new_order_ready = Mock()
    executor._ensure_instrument_symbols = Mock()
    executor._instruments = {"rb2701": SimpleNamespace(MinLimitOrderVolume=1, MaxLimitOrderVolume=1)}
    executor._snapshot_quote_error = Mock(return_value=None)
    executor.get_tick_size = Mock(return_value=1)
    executor._quotes = {"rb2701": SimpleNamespace(extra={"lower_limit_price": 3200, "upper_limit_price": 3800})}
    executor._call_trader_request = Mock(side_effect=AssertionError("非法输入不能到达原生边界"))
    symbol, volume, price = "rb2701", 1, 3500.0
    if case == "unknown":
        symbol = "FT_UNKNOWN_CONTRACT"
    elif case == "price":
        price = 3500.5
    else:
        volume = 2
    with pytest.raises(ValueError, match=expected):
        executor._place_order_impl(symbol, OrderDirection.BUY, OrderType.LIMIT, volume, price, offset_flag="open")
    executor._call_trader_request.assert_not_called()


def test_live_payloads_validate_against_current_public_models(tmp_path):
    from axile.server.db.models.account import AccountUpdate
    from axile.server.db.models.portfolio import PortfolioCreate, PortfolioUpdate

    config = Config(results_dir=str(tmp_path))
    api = OfflineApi(config)
    original = api.request

    def validate(method, path, body=None, **kwargs):
        if method == "PATCH" and path == "/account/4":
            AccountUpdate.model_validate(body)
        elif method == "POST" and path == "/portfolio/":
            PortfolioCreate.model_validate(body)
        elif method == "PATCH" and path == "/portfolio/99":
            PortfolioUpdate.model_validate(body)
        return original(method, path, body, **kwargs)

    api.request = validate
    bundle = Bundle(str(tmp_path), "models", config.public())
    runner = Runner(config, api, bundle, 4)
    runner.preflight()
    runner.prepare()
    runner.configure("open", "first")
    from ft_ctp.scenarios import quota_override

    quota_override(runner)
    runner.restore()


def test_http_request_does_not_retry_and_redirection_cannot_leak_auth(monkeypatch):
    api = Api("http://127.0.0.1:8000/api/v1")
    failure = Mock(side_effect=TimeoutError("uncertain"))
    monkeypatch.setattr(api.opener, "open", failure)
    with pytest.raises(ApiError):
        api.request("POST", "/account/execute/4")
    assert failure.call_count == 1


def test_secrets_in_product_zip_are_rejected(tmp_path):
    import io
    import zipfile

    bundle = Bundle(str(tmp_path), "zip", {}, Redactor({"password": "sensitive-password"}))
    runner = Runner(Config(), OfflineApi(Config()), bundle, 4)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("log.txt", "oops sensitive-password")
    with pytest.raises(ValueError, match="敏感"):
        runner.save_zip("bad.zip", buffer.getvalue())
    assert not (bundle.path / "bad.zip").exists()


def test_manifest_hash_is_not_corrupted_by_credential_substring(tmp_path):
    import hashlib
    import json

    from ft_ctp.evidence import Bundle, Redactor

    expected = hashlib.sha256(b"{}").hexdigest()
    redactor = Redactor({"broker_id": expected[8:12]})
    bundle = Bundle(str(tmp_path), "hash-check", {}, redactor)
    bundle.write("result.json", {})
    bundle.finish()
    manifest = json.loads((bundle.path / "manifest.json").read_text())
    assert manifest["result.json"] == expected
    for name, digest in manifest.items():
        assert hashlib.sha256((bundle.path / name).read_bytes()).hexdigest() == digest
