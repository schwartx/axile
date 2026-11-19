"""飞书自定义机器人最小 HTTP 客户端."""

import json
from http.client import HTTPException
from typing import Final
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_FEISHU_HOOK_BASE: Final = "https://open.feishu.cn/open-apis/bot/v2/hook/"
_FEISHU_TIMEOUT_SECONDS: Final = 15


def push_feishu_card(card: dict[str, object], key: str, *, timeout: float = _FEISHU_TIMEOUT_SECONDS) -> None:
    """向飞书自定义机器人发送交互卡片.

    Parameters
    ----------
    card : dict[str, object]
        飞书交互卡片内容。
    key : str
        飞书群自定义机器人 webhook key。
    timeout : float, default=15
        HTTP 请求超时时间（秒）。

    Raises
    ------
    RuntimeError
        HTTP 请求失败、飞书响应无法解析，或响应体表示发送失败。
    """
    request = Request(
        f"{_FEISHU_HOOK_BASE}{key}",
        data=json.dumps({"msg_type": "interactive", "card": card}, allow_nan=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        exc.close()
        raise RuntimeError(f"飞书请求失败（HTTP {exc.code}）") from None
    except (OSError, HTTPException):
        raise RuntimeError("飞书请求失败，请检查网络和 Webhook 配置") from None
    except ValueError:
        raise RuntimeError("飞书返回了无法解析的响应") from None

    if isinstance(result, dict) and (result.get("code") == 0 or result.get("StatusMessage") == "success"):
        return

    detail = result.get("msg") or result.get("StatusMessage") or result if isinstance(result, dict) else result
    message = str(detail)
    if key:
        message = message.replace(key, "[已隐藏]")
    raise RuntimeError(f"飞书返回失败: {message[:200]}")
