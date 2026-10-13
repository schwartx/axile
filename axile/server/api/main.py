"""组装顶层 FastAPI API 路由."""

from fastapi import APIRouter

from axile.server.api.routes import (
    account,
    algorithms,
    capabilities,
    connection_validation,
    editor,
    init,
    portfolio,
    utils,
)

api_router = APIRouter()
api_router.include_router(utils.router)
api_router.include_router(capabilities.router)
api_router.include_router(connection_validation.router)
api_router.include_router(connection_validation.account_router)
api_router.include_router(algorithms.router)
api_router.include_router(init.router)
api_router.include_router(account.router)
api_router.include_router(portfolio.router)
api_router.include_router(editor.router)
