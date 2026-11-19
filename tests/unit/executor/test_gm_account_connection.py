"""终端登录的目标校验、未知写入结果回读和重复点击边界。"""

import json
from email.message import Message
from io import BytesIO
from unittest.mock import Mock
from urllib.request import HTTPHandler
from urllib.response import addinfourl

import pytest

from axile.executor.gm.account_connection import GMAccountConnectError, can_connect_account, connect_account
from axile.executor.models.unified_input_accounts import GMAccountConfig


@pytest.fixture
def terminal(monkeypatch):
    from axile.executor.gm import account_connection

    opener = Mock()
    monkeypatch.setattr(account_connection, "build_opener", lambda *handlers: opener)
    config = GMAccountConfig(connection_mode="service", serv_addr="127.0.0.1:7001", account_id="target", token="secret")
    target = {"data": [{"account": {"account_id": "target"}, "channel": {"conn_type": 1, "channel_id": "gm-broker-1"}}]}

    def setup(*values):
        opener.open.side_effect = [
            value if isinstance(value, Exception) else addinfourl(BytesIO(json.dumps(value).encode()), {}, "", 200)
            for value in values
        ]

    return config, opener, target, setup


def state(value):
    return {"account_id": "target", "status": {"state": value}}


def test_explicit_login_rechecks_identity_and_requires_confirmed_login(terminal):
    config, opener, target, setup = terminal
    setup(target, state(5), {}, state(3))
    connect_account(config)
    requests = [call.args[0] for call in opener.open.call_args_list]
    assert [request.get_method() for request in requests] == ["GET", "GET", "POST", "GET"]
    assert json.loads(requests[2].data)["accountId"] == "target"
    assert requests[2].get_header("Content-type") == "application/json"
    assert requests[1].full_url.endswith("account-connection-status?accountId=target")
    assert all(request.get_header("Authorization") == "Bearer secret" for request in requests)
    assert all(call.kwargs["timeout"] == 2 for call in opener.open.call_args_list)


def test_already_logged_in_does_not_repeat_login(terminal):
    config, opener, target, setup = terminal
    setup(target, state(3))
    connect_account(config)
    assert [call.args[0].get_method() for call in opener.open.call_args_list] == ["GET", "GET"]


@pytest.mark.parametrize("after", [3, 5])
def test_unknown_login_result_is_read_back_without_retry(terminal, after):
    config, opener, target, setup = terminal
    setup(target, state(5), TimeoutError("secret"), state(after))
    if after == 3:
        connect_account(config)
    else:
        with pytest.raises(GMAccountConnectError) as exc:
            connect_account(config)
        assert "secret" not in str(exc.value)
    assert [call.args[0].get_method() for call in opener.open.call_args_list] == ["GET", "GET", "POST", "GET"]


def test_live_or_wrong_account_never_offers_or_executes_passwordless_login(terminal):
    config, opener, target, setup = terminal
    target["data"][0]["channel"]["conn_type"] = 2
    setup(target, target)
    assert not can_connect_account(config.token, config.account_id, config.serv_addr)
    with pytest.raises(GMAccountConnectError):
        connect_account(config)
    assert [call.args[0].get_method() for call in opener.open.call_args_list] == ["GET", "GET"]


def test_custom_rpc_gateway_is_not_guessed(terminal):
    config, opener, _, _ = terminal
    assert not can_connect_account(config.token, config.account_id, "127.0.0.1:8001")
    opener.open.assert_not_called()


def test_gateway_ignores_environment_proxy_and_refuses_redirect(monkeypatch):
    from axile.executor.gm.account_connection import GMTerminalConnection

    requests = []

    def redirect(self, request):
        requests.append(request)
        headers = Message()
        headers["Location"] = "http://redirect.invalid/"
        response = addinfourl(BytesIO(), headers, request.full_url, 302)
        response.msg = "Found"
        return response

    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr(HTTPHandler, "http_open", redirect)
    connection = GMTerminalConnection("secret", "target", "terminal.invalid:7001")

    with pytest.raises(GMAccountConnectError, match="HTTP 302"):
        connection.target()

    assert [request.host for request in requests] == ["terminal.invalid:7002"]
