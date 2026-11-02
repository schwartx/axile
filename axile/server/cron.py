"""CRON 表达式解析与账户排程触发器组装."""

from __future__ import annotations

from collections.abc import Sequence
from zoneinfo import ZoneInfo

from apscheduler.triggers.combining import OrTrigger  # type: ignore[import-not-found]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-not-found]

SCHEDULER_TIMEZONE = ZoneInfo("Asia/Shanghai")


class CompactOrTrigger(OrTrigger):
    """账户排程用的 ``OrTrigger``，把日志里的 trigger 文本压成条数.

    APScheduler 的 ``Job.__str__`` 会把 ``trigger`` 全文嵌进
    ``Job "..." executed successfully``。账户 cron 常被展开成几十条 crontab，
    默认 ``or[cron[...], cron[...], ...]`` 会淹没控制台。

    ``__str__`` 只保留条数，条数相同并不代表排程相同；对齐任务时必须用
    :func:`cron_triggers_equivalent` 比较子触发器。
    """

    __slots__ = ()

    def __str__(self) -> str:
        """返回 ``or[N crontab]``，供 APScheduler 把 job 写进日志."""
        return f"or[{len(self.triggers)} crontab]"


def is_blank_cron_expr(cron_expr: str | None) -> bool:
    """
    判断 cron 是否视为「无排程」（仅手动 / 外接触发）.

    前端关闭「自动调仓」时会把 ``cron_expr`` 写成空串；空串合法且表示不建调度任务，
    不得再交给 :func:`parse_cron_expr`（其会抛「空表达式」）。

    Parameters
    ----------
    cron_expr : str | None
        账户上存储的表达式；``None`` 或仅空白均视为无排程。

    Returns
    -------
    bool
        为真时应跳过调度解析与建 job。
    """
    return cron_expr is None or not str(cron_expr).strip()


def parse_cron_expr(cron_expr: str) -> list[CronTrigger]:
    """
    解析一个或多个 CRON 表达式.

    Parameters
    ----------
    cron_expr : str
        使用 ``|`` 分隔的单个或多个 crontab 表达式。

    Returns
    -------
    list[CronTrigger]
        解析后的 APScheduler CRON 触发器列表。

    Raises
    ------
    ValueError
        当表达式为空或任一子表达式无法解析时抛出。
        无排程场景请先用 :func:`is_blank_cron_expr` 短路，不要调用本函数。
    """
    if is_blank_cron_expr(cron_expr):
        raise ValueError("空表达式")

    try:
        cron_parts = [ce.strip() for ce in cron_expr.split("|") if ce.strip()]
        if not cron_parts:
            raise ValueError("空表达式")
        return [CronTrigger.from_crontab(ce, timezone=SCHEDULER_TIMEZONE) for ce in cron_parts]  # type: ignore[misc]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"解析CRON表达式失败: {exc}") from exc


def combine_cron_triggers(triggers: Sequence[CronTrigger]) -> CompactOrTrigger:
    """把多条 crontab 合成账户调度使用的压缩 ``OrTrigger``.

    Parameters
    ----------
    triggers : Sequence[CronTrigger]
        :func:`parse_cron_expr` 解析出的触发器列表。

    Returns
    -------
    CompactOrTrigger
        调度语义与 ``OrTrigger`` 相同，但 ``str()`` 只报告子触发器条数。
    """
    return CompactOrTrigger(list(triggers))


def cron_triggers_equivalent(left: object, right: object) -> bool:
    """判断两个触发器是否表示同一组 crontab.

    :class:`CompactOrTrigger` 的 ``str()`` 只有条数，不能用来对齐任务。
    本函数比较子 ``CronTrigger`` 的字段文本；没有子触发器时退回整体 ``str()``.

    Parameters
    ----------
    left : object
        已登记的 APScheduler trigger，或测试里的同构替身。
    right : object
        账户当前 cron 组装出的期望 trigger。

    Returns
    -------
    bool
        两边子 crontab 顺序与字段均相同时为真。
    """
    return _cron_trigger_fingerprint(left) == _cron_trigger_fingerprint(right)


def _cron_trigger_fingerprint(trigger: object) -> tuple[str, ...]:
    children = getattr(trigger, "triggers", None)
    if isinstance(children, (list, tuple)) and children:
        return tuple(str(child) for child in children)
    return (str(trigger),)


def effective_triggers(cron_expr: str, channel: object, sessions=None) -> list[CronTrigger]:
    """合并普通节奏与独立 GM 阶段触发；空 Cron 始终表示关闭自动调仓。"""
    if is_blank_cron_expr(cron_expr):
        return []
    triggers = parse_cron_expr(cron_expr)
    if str(channel) == "gm" and sessions is not None:
        for value in sessions.model_dump().values():
            if value["enabled"]:
                hour, minute = value["time"].split(":")
                triggers.extend(parse_cron_expr(f"{int(minute)} {int(hour)} * * *"))
    return list({str(trigger): trigger for trigger in triggers}.values())
