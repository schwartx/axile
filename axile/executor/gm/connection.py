"""GM 已确认连接事实的操作员说明，不解释 SDK 错误文本。"""

from collections.abc import Mapping, Sequence

from axile.executor.connection_presentation import connection_problem
from axile.executor.connection_state import ConnectionMonitor
from axile.executor.gm.common import AccConnectionState


class GMAccountStartupError(RuntimeError):
    """交易账户状态已明确异常，但不据此推断 Token 是否有效。"""

    def __init__(self, account_id: str, status: dict) -> None:
        """保留原生状态，并将已知状态与未知故障原因分开描述。"""
        state = status.get("state")
        state = int(state) if isinstance(state, int) else state
        state_label = (
            str(AccConnectionState(state)) if isinstance(state, int) and state in AccConnectionState else "未知"
        )
        reason = status.get("error") or status.get("error_msg") or status.get("message")
        self.execution_error = f"GM 交易账户{state_label}，登录未完成（状态码 {state}）"
        self.execution_error += f"：{reason}" if reason else "；SDK 未提供失败原因"
        super().__init__(f"{self.execution_error}: account_id={account_id}, status={status}")


def report_gm_failure(
    monitor: ConnectionMonitor,
    error: BaseException,
    label: str,
    summary: str,
    *,
    facts: Mapping[str, object] | None = None,
    steps: Sequence[str] = (),
    facts_title: str = "已确认的信息",
    steps_title: str = "建议检查",
) -> None:
    """展示构造延迟到观察边界，失败不影响原始故障。"""
    monitor.failed(
        error,
        label=label,
        presentation=lambda: connection_problem(
            label,
            summary,
            facts=facts,
            steps=steps,
            facts_title=facts_title,
            steps_title=steps_title,
        ),
    )


def report_account_status(monitor: ConnectionMonitor, error: BaseException, account_id: str, status: dict) -> None:
    """账户原生状态只解释目标账户，不推断终端进程状态。"""
    state = status.get("state")
    labels: dict[int, str] = {
        AccConnectionState.DISCONNECTING: "账户正在断开",
        AccConnectionState.DISCONNECTED: "账户已断开",
        AccConnectionState.ERROR: "账户状态异常",
    }
    label = labels.get(state, "账户状态异常") if isinstance(state, int) else "账户状态异常"
    report_gm_failure(
        monitor,
        error,
        label,
        "已收到目标交易账户的状态信息，当前账户未处于已登录状态。",
        facts={
            "目标账户": account_id,
            "账户状态": state,
            "状态说明": status.get("error") or status.get("error_msg") or status.get("message"),
        },
        facts_title="账户状态",
        steps_title="账户登录检查",
        steps=("检查该交易账户在当前 GM 服务中的登录情况。", "核对账户 ID 和该账户的访问权限。"),
    )


def report_runtime_failure(monitor: ConnectionMonitor, error: BaseException, *, authentication: bool = False) -> None:
    """明确鉴权证据与运行线程异常分别展示，1026 不推断为 Token 失效。"""
    if authentication or getattr(error, "status", None) == 1000:
        error.execution_error = "GM token 无效或已失效"  # type: ignore[attr-defined]
        report_gm_failure(
            monitor,
            error,
            "Token 无效",
            "GM SDK 明确报告 Token 无效或已失效。",
            facts={"鉴权证据": getattr(error, "status", None) or type(error).__name__},
            facts_title="鉴权证据",
            steps_title="Token 检查",
            steps=("核对当前 GM Token 是否有效。", "更新连接设置中的 Token 后重新启动账户运行时。"),
        )
        return
    if getattr(error, "status", None) == 1026:
        # 1026 确认的是令牌更新失败；是否凭证失效仍缺少证据。
        error.execution_error = "GM 令牌更新失败（错误码 1026）"  # type: ignore[attr-defined]
        report_gm_failure(
            monitor,
            error,
            "令牌更新失败",
            "GM SDK 报告令牌更新失败（错误码 1026）。尚无法确认是 Token 本身、GM 服务还是网络问题。",
            facts={
                "失败阶段": "令牌更新",
                "错误码": 1026,
                "SDK 说明": getattr(error, "message", None) or "更新令牌错误",
                "调用": getattr(error, "function", None),
            },
            facts_title="已确认的信息",
            steps_title="令牌更新检查",
            steps=(
                "核对当前配置的 GM Token。该错误不能单独证明 Token 已失效。",
                "检查 GM 服务和网络访问情况，处理后重新连接账户。",
            ),
        )
        return
    report_gm_failure(
        monitor,
        error,
        "交易运行时异常",
        "GM 策略运行线程报告异常，具体原因请查看技术详情。",
        facts={"失败阶段": "策略运行线程", "异常类型": type(error).__name__},
        facts_title="运行时信息",
        steps_title="恢复检查",
        steps=("检查 GM 交易服务和网络访问情况。", "根据技术详情处理故障后重新启动账户运行时。"),
    )
