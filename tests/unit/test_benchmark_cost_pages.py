"""分页基准使用服务端游标，保留第二页及原始响应证据。"""

import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import benchmark_cost_pages as benchmark


@pytest.mark.parametrize("fault", [None, "missing_cursor", "http_error"])
def test_client_uses_each_rounds_first_page_cursor_and_retains_workload(monkeypatch, tmp_path, fault):
    queries = []
    bodies = []
    closed = []

    def request(method, path):
        assert method == "GET"
        assert urlsplit(path).path == "/api/v1/account/performance/2/costs"
        queries.append(parse_qs(urlsplit(path).query))

    def response():
        index = len(queries) - 1
        first = index % 6 == 0
        token = f"server/{index // 6}/opaque+=20" if first else "wrong-filter-scope"
        body = {"data": [{"symbol": "POC_001", "cost": None}], "count": 30, "next_cursor": token}
        if fault == "missing_cursor" and first:
            body["next_cursor"] = None
        raw = json.dumps(body).encode()
        bodies.append(raw)
        return SimpleNamespace(status=503 if fault == "http_error" else 200, read=lambda: raw)

    connection = SimpleNamespace(request=request, getresponse=response, close=lambda: closed.append(True))
    monkeypatch.setattr(benchmark.http.client, "HTTPConnection", lambda *_args, **_kwargs: connection)
    output = tmp_path / "client.json"
    if fault:
        error, message = (RuntimeError, "HTTP 503") if fault == "http_error" else (ValueError, "second")
        with pytest.raises(error, match=message):
            benchmark.client(12345, "frozen-snapshot", output)
        assert len(queries) == (1 if fault == "http_error" else 5)
        assert not output.exists()
    else:
        benchmark.client(12345, "frozen-snapshot", output)
        saved = json.loads(output.read_text())
        assert len(queries) == len(saved) == 18
        for repeat in range(3):
            group = queries[repeat * 6 : (repeat + 1) * 6]
            assert all(query["snapshot_id"] == ["frozen-snapshot"] for query in group)
            assert all("cursor" not in query for query in group[:5])
            assert group[1]["sort"] == ["cost"]
            assert group[2]["side"] == ["buy"]
            assert group[3]["side"] == ["sell"]
            assert group[4]["symbol_search"] == ["POC_00"]
            assert group[5]["cursor"] == [f"server/{repeat}/opaque+=20"]
            assert saved[repeat * 6 + 5]["query"] == {"offset": 20}
        for index, row in enumerate(saved):
            body = json.loads(bodies[index])
            canonical = json.dumps(body, sort_keys=True, allow_nan=False).encode()
            assert row["repeat"] == index // 6
            assert row["bytes"] == len(bodies[index])
            assert row["sha256"] == hashlib.sha256(canonical).hexdigest()
            assert row["response"] == (body if index < 6 else None)
    assert closed == [True]
