"""掘金终端的显式仿真账户登录；不会由查询或健康检查触发。"""

import json
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from axile.executor.models.unified_input_accounts import BaseAccountConfig, GMAccountConfig


class GMAccountConnectError(ValueError):
    """可直接向操作员展示的终端连接错误。"""

    def __init__(self, message: str) -> None:
        self.execution_error = message
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """保留终端原始状态码，禁止跟随重定向。"""
        return None


class GMTerminalConnection:
    """只访问与配置的 RPC 同主机的标准终端网关。"""

    def __init__(self, token: str, account_id: str, serv_addr: str | None) -> None:
        address = urlsplit(f"http://{serv_addr or '127.0.0.1:7001'}")
        # shortcut: 只支持终端标准端口，支持自定义网关时需由渠道增加地址配置。
        if address.port != 7001 or not address.hostname or address.username or address.password:
            raise GMAccountConnectError("当前终端地址不支持一键连接，请在掘金终端中连接账户")
        host = f"[{address.hostname}]" if ":" in address.hostname else address.hostname
        self.base_url = f"http://{host}:7002"
        self.account_id = account_id
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())
        self.token = token

    def request(
        self, method: str, path: str, *, params: dict[str, str] | None = None, payload: dict[str, object] | None = None
    ) -> dict:
        """限制重定向及等待时间，不将含凭据的请求异常暴露给业务界面。"""
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, allow_nan=False).encode()
        try:
            with self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=2) as response:
                status = response.status
                result = json.load(response) if status == 200 else None
        except HTTPError as exc:
            exc.close()
            status, result = exc.code, None
        except (OSError, HTTPException, ValueError):
            raise GMAccountConnectError("无法确认掘金终端的连接结果，请检查终端账户状态后再操作") from None
        if status != 200:
            raise GMAccountConnectError(f"掘金终端请求失败（HTTP {status}），请检查终端账户连接")
        if not isinstance(result, dict):
            raise GMAccountConnectError("掘金终端返回了无效的账户连接结果")
        return result

    def target(self) -> dict:
        """核对目标身份及渠道，拒绝将无密码仿真登录用于其他账户。"""
        accounts = self.request("GET", "/v5/term/account/accounts").get("data", [])
        for item in accounts:
            if item.get("account", {}).get("account_id") == self.account_id:
                channel = item.get("channel", {})
                if channel.get("conn_type") == 1 and channel.get("channel_id", "").startswith("gm-broker-"):
                    return item["account"]
                break
        raise GMAccountConnectError("该账户不支持一键连接，请在掘金终端中完成账户登录")

    def status(self) -> dict:
        """读取当前目标账户状态。"""
        result = self.request("GET", "/v5/term/account-connection-status", params={"accountId": self.account_id})
        if result.get("account_id") != self.account_id:
            raise GMAccountConnectError("掘金终端返回的账户与目标账户不一致")
        return result.get("status", {})


def can_connect_account(token: str, account_id: str, serv_addr: str | None) -> bool:
    """只有验证过目标身份的仿真账户才提供按钮；探测失败不覆盖原始断开事实。"""
    try:
        GMTerminalConnection(token, account_id, serv_addr).target()
    except (ValueError, TypeError, KeyError):
        return False
    return True


def connect_account(config: BaseAccountConfig) -> None:
    """用户显式连接一次；响应未知时只回读，绝不自动重发登录。"""
    if not isinstance(config, GMAccountConfig):
        raise TypeError("GM 账户连接需要掘金配置")
    terminal = GMTerminalConnection(config.token, config.account_id, config.serv_addr)
    account = terminal.target()
    if terminal.status().get("state") == 3:
        return
    request_error = None
    try:
        terminal.request(
            "POST",
            "/v3/account-trade/login/" + quote(config.account_id, safe=""),
            payload={
                "accountId": config.account_id,
                "accountName": account.get("account_name", ""),
                "password": "",
                "useCachedCredentail": False,
                "properties": {},
            },
        )
    except ValueError as error:
        request_error = error
    status = terminal.status()
    if status.get("state") != 3:
        reason = (status.get("error") or {}).get("info")
        raise GMAccountConnectError(
            str(reason).replace(config.token, "[已隐藏]")
            if reason
            else str(request_error or "掘金账户未能完成登录，请在终端查看原因")
        )
