"""终端登录的目标校验、未知写入结果回读和重复点击边界。"""

from unittest.mock import Mock

import pytest
import requests

from axile.executor.gm.account_connection import GMAccountConnectError, can_connect_account, connect_account
from axile.executor.models.unified_input_accounts import GMAccountConfig


@pytest.fixture
def terminal(monkeypatch):
    from axile.executor.gm import account_connection

    session = Mock()
    session.headers = {}
    monkeypatch.setattr(account_connection.requests, "Session", lambda: session)
    config = GMAccountConfig(connection_mode="service", serv_addr="127.0.0.1:7001", account_id="target", token="secret")
    target = {"data": [{"account": {"account_id": "target"}, "channel": {"conn_type": 1, "channel_id": "gm-broker-1"}}]}

    def respond(value):
        return Mock(status_code=200, json=lambda: value)

    def setup(*values):
        session.request.side_effect = [x if isinstance(x, Exception) else respond(x) for x in values]

    return config, session, target, setup


def state(value):
    return {"account_id": "target", "status": {"state": value}}


def test_explicit_login_rechecks_identity_and_requires_confirmed_login(terminal):
    config, session, target, setup = terminal
    setup(target, state(5), {}, state(3))
    connect_account(config)
    calls = session.request.call_args_list
    assert [x.args[0] for x in calls] == ["GET", "GET", "POST", "GET"]
    assert calls[2].kwargs["json"]["accountId"] == "target"
    assert session.trust_env is False
    assert all(x.kwargs["allow_redirects"] is False for x in calls)
    session.close.assert_called_once()


def test_already_logged_in_does_not_repeat_login(terminal):
    config, session, target, setup = terminal
    setup(target, state(3))
    connect_account(config)
    assert all(x.args[0] == "GET" for x in session.request.call_args_list)


@pytest.mark.parametrize("after", [3, 5])
def test_unknown_login_result_is_read_back_without_retry(terminal, after):
    config, session, target, setup = terminal
    setup(target, state(5), requests.Timeout("secret"), state(after))
    if after == 3:
        connect_account(config)
    else:
        with pytest.raises(GMAccountConnectError) as exc:
            connect_account(config)
        assert "secret" not in str(exc.value)
    assert sum(x.args[0] == "POST" for x in session.request.call_args_list) == 1


def test_live_or_wrong_account_never_offers_or_executes_passwordless_login(terminal):
    config, session, target, setup = terminal
    target["data"][0]["channel"]["conn_type"] = 2
    setup(target, target)
    assert not can_connect_account(config.token, config.account_id, config.serv_addr)
    with pytest.raises(GMAccountConnectError):
        connect_account(config)
    assert all(x.args[0] == "GET" for x in session.request.call_args_list)


def test_custom_rpc_gateway_is_not_guessed(terminal):
    config, session, _, _ = terminal
    assert not can_connect_account(config.token, config.account_id, "127.0.0.1:8001")
    session.request.assert_not_called()
