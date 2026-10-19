"""服务端数据库模型聚合入口."""

from __future__ import annotations

from sqlmodel import SQLModel

from axile.server.db.models import analysis as analysis
from axile.server.db.models.account import (
    Account,
    AccountBase,
    AccountCreate,
    AccountDashboardItemPublic,
    AccountDashboardPublic,
    AccountListPublic,
    AccountNextRunPublic,
    AccountPublic,
    AccountRebalancePlanPublic,
    AccountRebalancePlanRowPublic,
    AccountSnapshot,
    AccountUpdate,
    PortfolioAccount,
    PortfolioAccountBase,
    PortfolioAccountListPublic,
    PortfolioAccountPublic,
)
from axile.server.db.models.account_asset import (
    AccountAssetSnapshot,
    AccountAssetSnapshotListPublic,
    AccountAssetSnapshotPublic,
    AccountAssetSnapshotSource,
)
from axile.server.db.models.account_connection import AccountConnectionState
from axile.server.db.models.account_control import (
    AccountControlAuditExecutionListPublic,
    AccountControlAuditExecutionPublic,
    AccountControlAuditSummaryPublic,
    AccountControlCounterDelta,
    AccountControlCounterDeltaBase,
    AccountControlEvent,
    AccountControlEventBase,
    AccountControlEventListPublic,
    AccountControlEventPublic,
)
from axile.server.db.models.account_control_session import AccountControlSession
from axile.server.db.models.account_notification import AccountNotificationState, AccountNotificationStatePublic
from axile.server.db.models.account_runtime_sync import (
    AccountRuntimeSync,
    AccountRuntimeSyncAttempt,
    AccountRuntimeSyncPublic,
)
from axile.server.db.models.account_settings import AccountSettings
from axile.server.db.models.base import PydanticJSONType, new_execution_id, now_ms, now_str
from axile.server.db.models.execution import (
    ExecuteRecord,
    ExecuteRecordBase,
    ExecuteRecordListPublic,
    ExecuteRecordPublic,
    ExecutionArtifact,
    ExecutionArtifactBase,
    ExecutionArtifactListPublic,
    ExecutionArtifactPublic,
    ExecutionEvent,
    ExecutionEventBase,
    ExecutionEventListPublic,
    ExecutionEventPublic,
    ExecutionStatusPublic,
    ExecutionTerminateRequest,
    ExecutionTerminateResponse,
    ExecutionTriggerResponse,
    ListResponse,
    ListResponsePage,
    Message,
)
from axile.server.db.models.execution_intent import ExecutionIntent
from axile.server.db.models.order_submission import OrderSubmission
from axile.server.db.models.portfolio import (
    Portfolio,
    PortfolioBase,
    PortfolioCreate,
    PortfolioListPublic,
    PortfolioLitePublic,
    PortfolioPublic,
    PortfolioUpdate,
    ValidateCustomCalcRequest,
    ValidateCustomCalcResponse,
)
from axile.server.db.models.schedule import (
    AccountActivityListPublic,
    ExecutionActivity,
    ScheduleSkip,
    ScheduleSkipActivity,
)
from axile.server.db.models.supplement import NotificationEvent, SupplementGroup, SupplementStep
from axile.server.db.models.system_notification import SystemNotificationState, SystemNotificationStatePublic
from axile.server.db.models.target_weight_snapshot import (
    TargetSizingAvailability,
    TargetSizingPublic,
    TargetSizingRowPublic,
    TargetWeightSnapshot,
    TargetWeightSnapshotPublic,
    TargetWeightSnapshotSource,
)
from axile.server.db.models.time_check import TimeCheckSettingsRow, TimeCheckStateRow

Portfolio.model_rebuild()
Account.model_rebuild()
AccountAssetSnapshot.model_rebuild()
PortfolioAccount.model_rebuild()

ExecuteRecord.model_rebuild()


__all__ = [
    "OrderSubmission",
    "AccountConnectionState",
    "NotificationEvent",
    "SupplementGroup",
    "SupplementStep",
    "SystemNotificationState",
    "SystemNotificationStatePublic",
    "AccountSettings",
    "AccountSnapshot",
    "AccountNotificationState",
    "AccountNotificationStatePublic",
    "SQLModel",
    "PydanticJSONType",
    "now_str",
    "now_ms",
    "new_execution_id",
    "PortfolioBase",
    "Portfolio",
    "PortfolioUpdate",
    "PortfolioCreate",
    "PortfolioPublic",
    "PortfolioLitePublic",
    "PortfolioListPublic",
    "TargetWeightSnapshot",
    "TargetWeightSnapshotPublic",
    "TargetWeightSnapshotSource",
    "TargetSizingAvailability",
    "TargetSizingPublic",
    "TargetSizingRowPublic",
    "ValidateCustomCalcRequest",
    "ValidateCustomCalcResponse",
    "AccountBase",
    "AccountCreate",
    "Account",
    "AccountPublic",
    "AccountListPublic",
    "AccountNextRunPublic",
    "AccountDashboardItemPublic",
    "AccountDashboardPublic",
    "AccountRebalancePlanPublic",
    "AccountRebalancePlanRowPublic",
    "AccountUpdate",
    "AccountRuntimeSync",
    "AccountRuntimeSyncAttempt",
    "AccountRuntimeSyncPublic",
    "AccountAssetSnapshot",
    "AccountAssetSnapshotPublic",
    "AccountAssetSnapshotListPublic",
    "AccountAssetSnapshotSource",
    "PortfolioAccountBase",
    "PortfolioAccount",
    "PortfolioAccountPublic",
    "PortfolioAccountListPublic",
    "ExecuteRecordBase",
    "ExecuteRecord",
    "ExecutionIntent",
    "ExecuteRecordPublic",
    "ExecuteRecordListPublic",
    "ExecutionEventBase",
    "ExecutionEvent",
    "ExecutionEventPublic",
    "ExecutionEventListPublic",
    "ExecutionArtifactBase",
    "ExecutionArtifact",
    "ExecutionArtifactPublic",
    "ExecutionArtifactListPublic",
    "AccountControlCounterDeltaBase",
    "AccountControlCounterDelta",
    "AccountControlEventBase",
    "AccountControlEvent",
    "AccountControlSession",
    "AccountControlAuditSummaryPublic",
    "AccountControlAuditExecutionPublic",
    "AccountControlAuditExecutionListPublic",
    "AccountControlEventPublic",
    "AccountControlEventListPublic",
    "ExecutionTriggerResponse",
    "ExecutionTerminateRequest",
    "ExecutionTerminateResponse",
    "ExecutionStatusPublic",
    "Message",
    "ListResponse",
    "ListResponsePage",
    "AccountActivityListPublic",
    "ExecutionActivity",
    "ScheduleSkip",
    "ScheduleSkipActivity",
]

__all__ += ["TimeCheckSettingsRow", "TimeCheckStateRow"]
