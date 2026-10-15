"""本机目录选择器只读 API 测试."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.routes import utils


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(utils.router, prefix="/api/v1")
    return TestClient(app)


def test_directory_picker_lists_only_directories(tmp_path) -> None:
    (tmp_path / "Beta").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "ignored.txt").touch()

    response = _client().get("/api/v1/utils/directories", params={"path": str(tmp_path)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["path"] == str(tmp_path.resolve())
    assert [item["name"] for item in payload["entries"]] == ["alpha", "Beta"]
    assert payload["parent"] == str(tmp_path.resolve().parent)
