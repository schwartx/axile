"""账户相关路由的聚合入口."""

from fastapi import APIRouter

from axile.server.api.routes.account_assets import router as account_assets_router
from axile.server.api.routes.account_connection import router as account_connection_router
from axile.server.api.routes.account_control_audit_routes import router as account_control_audit_router
from axile.server.api.routes.account_control_monitor_routes import router as account_control_monitor_router
from axile.server.api.routes.account_control_policy_routes import router as account_control_policy_router
from axile.server.api.routes.account_crud import router as account_crud_router
from axile.server.api.routes.account_evidence import router as account_evidence_router
from axile.server.api.routes.account_execution import router as account_execution_router
from axile.server.api.routes.account_feishu import router as account_feishu_router
from axile.server.api.routes.account_orders import router as account_orders_router
from axile.server.api.routes.account_performance import router as account_performance_router
from axile.server.api.routes.account_schedule import router as account_schedule_router
from axile.server.api.routes.account_support import (
    _build_empty_positions_algorithm,
    _reconcile_account_job,
    _validate_account_control_binding,
)

router = APIRouter(prefix="/account", tags=["account"])
router.include_router(account_control_monitor_router)
router.include_router(account_evidence_router)
router.include_router(account_crud_router)
router.include_router(account_connection_router)
router.include_router(account_assets_router)
router.include_router(account_orders_router)
router.include_router(account_performance_router)
router.include_router(account_execution_router)
router.include_router(account_feishu_router)
router.include_router(account_schedule_router)
router.include_router(account_control_audit_router)
router.include_router(account_control_policy_router)

__all__ = [
    "router",
    "_build_empty_positions_algorithm",
    "_reconcile_account_job",
    "_validate_account_control_binding",
]
