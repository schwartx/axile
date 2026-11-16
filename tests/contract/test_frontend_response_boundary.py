"""前端 HTTP 路由使用公开投影；编辑入口独立校验输入。"""

import inspect

from fastapi.routing import APIRoute
from pydantic import BaseModel

from axile.server.api import main, routes


def test_frontend_routes_have_no_implicit_or_explicit_response_models():
    assert main.api_router.routes
    route_count = 0
    input_dependencies = 0
    for module in vars(routes).values():
        if not inspect.ismodule(module) or not module.__name__.startswith(f"{routes.__name__}."):
            continue
        for name in ("router", "account_router"):
            for route in getattr(getattr(module, name, None), "routes", []):
                if not isinstance(route, APIRoute):
                    continue
                route_count += 1
                assert route.response_model is None, (route.path, route.response_model)
                assert route.response_field is None, route.path
                for parameter in route.dependant.body_params:
                    model = parameter.field_info.annotation
                    if inspect.isclass(model) and issubclass(model, BaseModel):
                        raise AssertionError((route.path, model))
                input_dependencies += sum(
                    getattr(dependency.call, "__module__", "") == "axile.server.api.inputs"
                    for dependency in route.dependant.dependencies
                )
    assert route_count > 100
    assert input_dependencies > 20
