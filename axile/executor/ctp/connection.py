"""CTP 已确认阶段与原生故障的展示内容。"""

from collections.abc import Mapping, Sequence

from axile.executor.connection_presentation import connection_problem
from axile.executor.connection_state import ConnectionMonitor


def report_ctp_failure(
    monitor: ConnectionMonitor,
    error: BaseException,
    label: str,
    summary: str,
    *,
    facts: Mapping[str, object] | None = None,
    steps: Sequence[str] = (),
) -> None:
    """在已确认故障处提供内容，展示失败不覆盖柜台证据。"""
    monitor.failed(
        error,
        label=label,
        presentation=lambda: connection_problem(
            label,
            summary,
            facts=facts,
            steps=steps,
            facts_title="柜台证据",
            steps_title="会话检查",
        ),
    )


def report_ctp_stage(monitor: ConnectionMonitor, error: BaseException, stage: str, *, timeout: bool = False) -> None:
    """阶段失败只能证明阶段未完成，不猜测密码或认证码错误。"""
    label = f"{stage}{'超时' if timeout else '失败'}"
    report_ctp_failure(
        monitor,
        error,
        label,
        f"CTP {stage}阶段未完成，当前会话需要重建。具体原因以柜台证据为准。",
        facts={
            "失败阶段": stage,
            "返回码": getattr(error, "return_code", None),
            "柜台错误码": getattr(error, "error_id", None),
        },
        steps=(
            f"核对{stage}阶段所需的连接配置与网络访问。",
            "认证等待也包含交易前置建立连接，超时不能直接确认凭证错误。",
            "处理故障后创建新实例重新连接；发起连接本身不会确认恢复。",
        )
        if stage == "认证"
        else (f"核对{stage}阶段所需的连接配置与网络访问。", "处理故障后创建新实例重新连接；发起连接本身不会确认恢复。"),
    )
