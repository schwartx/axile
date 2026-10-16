"""交易选择、采集归属和脱敏的边界回归，不访问真实服务。"""

import json
from unittest.mock import Mock

import pytest

from ft_ctp.cli import main, parser, selected_config
from ft_ctp.config import Config
from ft_ctp.evidence import Bundle, Redactor
from ft_ctp.runner import Conditions, Runner


@pytest.mark.parametrize("symbol", [None, "", "  "])
def test_run_requires_explicit_nonempty_contract_before_credentials(tmp_path, monkeypatch, symbol):
    path = tmp_path / "local.json"
    path.write_text(json.dumps({} if symbol is None else {"symbol": symbol}))
    credentials = Mock(side_effect=AssertionError("不能读取凭据"))
    api = Mock(side_effect=AssertionError("不能访问 API"))
    monkeypatch.setattr("ft_ctp.cli.load_account", credentials)
    monkeypatch.setattr("ft_ctp.cli.Api", api)
    assert main(["run", "--config", str(path)]) == 2
    credentials.assert_not_called()
    api.assert_not_called()


@pytest.mark.parametrize("selection", ["config", "cli", "set"])
def test_contract_can_be_selected_explicitly(tmp_path, selection):
    path = tmp_path / "local.json"
    path.write_text(json.dumps({"symbol": "TEST_CONTRACT" if selection == "config" else ""}))
    args = ["run", "--config", str(path)]
    if selection == "cli":
        args += ["--symbol", "TEST_CONTRACT"]
    elif selection == "set":
        args += ["--set", "symbol=TEST_CONTRACT"]
    assert selected_config(parser().parse_args(args)).symbol == "TEST_CONTRACT"


def test_all_commands_require_local_configuration():
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["run"])
    assert error.value.code == 2


def test_short_credentials_preserve_native_identity_and_redact_fields():
    redact = Redactor({"broker_id": "1234", "password": "x", "auth_code": "long-secret-code"})
    data = {
        "order_id": "20260101:1234:1",
        "timestamp": "2026-01-01T12:34:00",
        "error_code": "1234",
        "broker_id": "1234",
        "password": "x",
        "message": 'broker_id=1234 password="x" failure long-secret-code',
    }
    result = redact(data)
    for key in ("order_id", "timestamp", "error_code"):
        assert result[key] == data[key]
    assert result["broker_id"] == result["password"] == "[REDACTED]"
    assert "1234" not in result["message"] and '"x"' not in result["message"]
    assert "long-secret-code" not in result["message"]


@pytest.mark.parametrize(
    "state,reason",
    [
        ({"account_id": 5, "status": "SUCCEEDED"}, "不属于"),
        ({"account_id": 4, "status": "RUNNING"}, "尚未结束"),
        ({"account_id": 4, "status": "QUEUED"}, "尚未结束"),
        ({"status": "SUCCEEDED"}, "不属于"),
    ],
)
def test_collection_rejects_wrong_account_and_nonterminal_before_artifacts(tmp_path, state, reason):
    api = Mock()
    api.request.return_value = state
    runner = Runner(Config(), api, Bundle(str(tmp_path), "collect", {}), 4)
    with pytest.raises(Conditions, match=reason):
        runner.collect("execution-id", "collected")
    api.request.assert_called_once_with("GET", "/account/executions/execution-id")
    api.pages.assert_not_called()
    assert not (runner.bundle.path / "collected").exists()


def test_collection_accepts_terminal_execution_for_selected_account(tmp_path):
    api = Mock()
    api.request.side_effect = [
        {"account_id": 4, "status": "FAILED"},
        RuntimeError("产品 ZIP 暂不可用"),
    ]
    api.pages.return_value = {"data": [], "complete": True}
    runner = Runner(Config(), api, Bundle(str(tmp_path), "collect", {}), 4)
    collected = runner.collect("execution-id", "collected")
    assert len(collected) == 3 and api.pages.call_count == 3
    assert (runner.bundle.path / "collected/status.json").exists()
    assert runner.bundle.index["missing"]
