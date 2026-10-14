"""TQ 运行阶段的说明，不从整体工厂异常猜测凭证或账户根因。"""

from axile.executor.connection_presentation import connection_problem
from axile.executor.connection_state import ConnectionMonitor


def report_tq_failure(monitor: ConnectionMonitor, error: BaseException, label: str, stage: str) -> None:
    """只描述已观察到的初始化或事件泵故障。"""
    monitor.failed(
        error,
        label=label,
        presentation=lambda: connection_problem(
            label,
            f"天勤{stage}未能完成，具体原因尚未确认。",
            facts={"失败阶段": stage, "异常类型": type(error).__name__},
            facts_title="运行阶段",
            steps_title="运行时检查",
            steps=("检查天勤连接配置和网络访问情况。", "根据技术详情处理故障后重新创建账户运行时。"),
        ),
    )
