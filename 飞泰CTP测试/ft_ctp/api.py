"""无自动重试的 REST 传输与分页读取。"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


class ApiError(RuntimeError):
    """保留 HTTP 状态，传输失败时不得推断写请求未执行。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止鉴权头经重定向流出目标服务。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Api:
    def __init__(self, url, timeout=10):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("API 地址格式不正确")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程 API 必须使用 HTTPS")
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None, binary=False):
        """写请求只发一次；401/超时等原样向上报告。"""
        headers = {"Accept": "application/zip" if binary else "application/json"}
        password = os.environ.get("AXON_API_PASSWORD") or os.environ.get("AXILE_API_PASSWORD")
        if password:
            headers["x-api-password"] = password
        payload = None if body is None else json.dumps(body).encode()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=max(self.timeout, 75) if binary else self.timeout) as response:
                data = response.read()
                return data if binary else json.loads(data)
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.read().decode(errors="replace"), exc.code) from exc
        except (OSError, ValueError) as exc:
            raise ApiError(str(exc)) from exc

    def pages(self, path, limit=200):
        """分页总数变化、重复项或空缺页面返回 incomplete，不冒充完整。"""
        rows, totals, seen = [], set(), set()
        skip = 0
        for _ in range(1000):
            separator = "&" if "?" in path else "?"
            page = self.request("GET", f"{path}{separator}skip={skip}&limit={limit}")
            data = page.get("data", [])
            totals.add(page.get("count"))
            for row in data:
                identity = json.dumps(row, sort_keys=True, default=str)
                if identity in seen:
                    return {"data": rows, "complete": False, "reason": "分页重复项"}
                seen.add(identity)
                rows.append(row)
            skip += len(data)
            count = page.get("count")
            if not data or (isinstance(count, int) and skip >= count) or (count is None and len(data) < limit):
                complete = len(totals) == 1 and isinstance(count, int) and skip == count
                return {"data": rows, "count": count, "complete": complete}
        return {"data": rows, "complete": False, "reason": "分页上限"}
