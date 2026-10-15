"""算法注册表查询路由测试.

Notes
-----
用仅挂载 ``algorithms.router`` 的最小 FastAPI 应用测试；该接口只读注册表、无副作用。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.server.api.routes import algorithms as algorithms_module


@pytest.fixture
def client() -> TestClient:
    """仅挂载算法列表路由的测试客户端."""
    app = FastAPI()
    app.include_router(algorithms_module.router, prefix="/api/v1")
    return TestClient(app)


def test_participation_display_scale_does_not_change_wire_constraints(client):
    item = next(item for item in client.get("/api/v1/algorithms").json() if item["name"] == "POV")
    rate = item["params_schema"]["properties"]["participation_rate"]
    assert rate["x-display-scale"] == 100
    assert rate["exclusiveMinimum"] == 0
    assert rate["maximum"] == 1
    assert rate["default"] == 0.1


def test_parameter_controls_keep_wire_contract(client):
    """组件提示覆盖内置算法；快捷值有效且不缩窄参与率精度。"""
    items = client.get("/api/v1/algorithms").json()
    for item in items:
        if item["name"] not in {"TWAP", "POV", "SINGLE-MAKER", "TARGET-POS-TASK", "CTP_OPTION_EXERCISE"}:
            continue
        fields = item["params_schema"]["properties"]
        for field in fields.values():
            if field["type"] in {"integer", "number"}:
                assert field["x-control"] in {"stepper", "numberflow", "presets", "slider"}
            if "x-enum-labels" in field:
                assert field["x-control"] in {"choice", "cards"}
            for preset in field.get("x-presets", []):
                assert field["minimum"] <= preset <= field["maximum"]
    pov = next(item for item in items if item["name"] == "POV")
    rate = pov["params_schema"]["properties"]["participation_rate"]
    assert rate["x-control"] == "numberflow"
    assert rate["x-display-step"] == 1
    assert "x-slider-min" not in rate
    assert "x-slider-max" not in rate
    assert "multipleOf" not in rate
