"""执行链路共享领域枚举定义."""

from enum import Enum


class ExecutionEventType(str, Enum):
    """审计流中记录的执行生命周期事件类型."""

    EXECUTION_STARTED = "execution_started"
    EXECUTION_TERMINATION_REQUESTED = "execution_termination_requested"
    EXECUTION_TERMINATION_ACKED = "execution_termination_acked"
    EXECUTION_TERMINATED = "execution_terminated"
    INPUT_SNAPSHOTTED = "input_snapshotted"
    INSTRUMENTS_WAITING = "instruments_waiting"
    INSTRUMENTS_READY = "instruments_ready"
    INSTRUMENTS_FAILED = "instruments_failed"
    TARGET_COMPUTED = "target_computed"
    FUTURES_CLOSE_PLANNED = "futures_close_planned"
    SYMBOL_DECISION_MADE = "symbol_decision_made"
    SYMBOL_SKIPPED = "symbol_skipped"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_ACKNOWLEDGED = "order_acknowledged"
    ORDER_TERMINAL = "order_terminal"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"


class ExecutionEventStatus(str, Enum):
    """执行事件使用的类似严重级别的状态标记."""

    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ExecutionReasonFamily(str, Enum):
    """执行事件使用的高层原因分类."""

    INPUT = "INPUT"
    RISK = "RISK"
    MARKET_RULE = "MARKET_RULE"
    ACCOUNT_STATE = "ACCOUNT_STATE"
    EXECUTION_STRATEGY = "EXECUTION_STRATEGY"
    EXCHANGE = "EXCHANGE"
    SYSTEM = "SYSTEM"


class ExecutionArtifactType(str, Enum):
    """一次执行过程中采集的快照附件类型."""

    STANDARD_INPUT = "standard_input"
    TARGET_SNAPSHOT = "target_snapshot"
    ACCOUNT_SNAPSHOT = "account_snapshot"
    ACCOUNT_SNAPSHOT_BEFORE = "account_snapshot_before"
    TRADE_RULES_SNAPSHOT = "trade_rules_snapshot"
    MARKET_SNAPSHOT = "market_snapshot"
    EXECUTION_SUMMARY = "execution_summary"
    ENVIRONMENT_SNAPSHOT = "environment_snapshot"


class ExecutionTaskStatus(str, Enum):
    """服务端后台任务生命周期状态."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    TERMINATING = "TERMINATING"
    TERMINATED = "TERMINATED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    PARTIAL = "PARTIAL"


class ExecutionTerminateMode(str, Enum):
    """终止执行时的控制模式."""

    GRACEFUL = "graceful"
    CANCEL_PENDING = "cancel_pending"


class ExecutionKind(str, Enum):
    """服务端 execution 的业务类型."""

    REBALANCE = "rebalance"
    CLEAR_POSITIONS = "clear_positions"
