import json
import traceback
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from axile.common import feishu


@pytest.mark.parametrize("result", [{"code": 0}, {"StatusMessage": "success"}])
def test_push_feishu_card_posts_json(monkeypatch, result):
    def open_card(request, *, timeout):
        assert request.get_method() == "POST"
        assert request.full_url.endswith("/hook/test-key")
        assert request.get_header("Content-type") == "application/json"
        assert timeout == 10
        assert json.loads(request.data) == {"msg_type": "interactive", "card": {"title": "测试"}}
        return BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(feishu, "urlopen", open_card)
    feishu.push_feishu_card({"title": "测试"}, "test-key", timeout=10)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (HTTPError("https://example.invalid/test-key", 403, "test-key", {}, None), "HTTP 403"),
        (URLError("test-key"), "飞书请求失败"),
        (b"malformed test-key", "无法解析"),
        (b'{"code": 1, "msg": "invalid test-key"}', "invalid \\[已隐藏\\]"),
    ],
)
def test_push_feishu_card_sanitizes_failures(monkeypatch, response, message):
    key = "test-key"

    def open_card(request, *, timeout):
        assert timeout == 15
        if isinstance(response, Exception):
            raise response
        return BytesIO(response)

    monkeypatch.setattr(feishu, "urlopen", open_card)
    with pytest.raises(RuntimeError, match=message) as exc:
        feishu.push_feishu_card({}, key)
    assert key not in "".join(traceback.format_exception(exc.value))
