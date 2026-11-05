"""GM 阶段权限、配置冻结与跨阶段失效。"""

from datetime import datetime, timedelta

import pytest

from axile.channels.gm_sessions import (
    SHANGHAI,
    GMSessionSettings,
    GMSessionTime,
    check_gm_operation,
    gm_phase,
    gm_session_context,
)
from axile.executor.order_cancellation import CancelNotAllowedError
from axile.server.cron import effective_triggers


def moment(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-09-01T{clock}:00").replace(tzinfo=SHANGHAI)


@pytest.mark.parametrize(
    ("clock", "phase", "cancelable"),
    [
        ("09:14", "closed", False),
        ("09:15", "opening_cancelable", True),
        ("09:19", "opening_cancelable", True),
        ("09:20", "opening_locked", False),
        ("09:24", "opening_locked", False),
        ("09:25", "opening_wait", False),
        ("09:29", "opening_wait", False),
        ("09:30", "continuous_0", True),
        ("11:30", "closed", False),
        ("13:00", "continuous_1", True),
        ("14:56", "continuous_1", True),
        ("14:57", "closing_auction", False),
        ("14:59", "closing_auction", False),
        ("15:00", "closed", False),
        ("15:05", "after_hours", False),
        ("15:30", "closed", False),
    ],
)
def test_phase_and_cancel_boundaries(clock, phase, cancelable):
    assert gm_phase(moment(clock)) == phase
    if cancelable:
        check_gm_operation(moment(clock), cancel=True)
    else:
        with pytest.raises(CancelNotAllowedError, match="不允许撤单"):
            check_gm_operation(moment(clock), cancel=True)


def test_auction_requires_frozen_permission_and_never_accepts_market_orders():
    now = moment("09:15")
    settings = GMSessionSettings(opening_cancelable={"enabled": True, "time": "09:15"})
    context = gm_session_context(settings.model_dump(), now)
    check_gm_operation(now, context=context)
    with pytest.raises(ValueError, match="不允许本次下单"):
        check_gm_operation(now)
    with pytest.raises(ValueError, match="仅允许限价单"):
        check_gm_operation(now, context=context, limit=False)
    for later in (moment("09:20"), moment("09:25"), moment("09:30"), now + timedelta(days=1)):
        with pytest.raises(ValueError, match="已结束"):
            check_gm_operation(later, context=context)
    with pytest.raises(ValueError):
        gm_session_context(GMSessionSettings().model_dump(), now)


@pytest.mark.parametrize(
    ("clock", "phase"),
    [("09:15", "opening_cancelable"), ("09:20", "opening_locked"), ("14:57", "closing_auction")],
)
def test_session_context_freezes_permissions_without_extending_algorithm_budget(clock, phase):
    settings = GMSessionSettings(**{phase: {"enabled": True, "time": clock}})
    context = gm_session_context(settings.model_dump(), moment(clock))
    assert context == {"phase": phase, "trading_day": "2026-09-01", "sessions": settings.model_dump()}


def test_effective_triggers_merge_once_without_turning_on_empty_cron():
    settings = GMSessionSettings(
        opening_cancelable={"enabled": True, "time": "09:15"},
        opening_locked={"enabled": True, "time": "09:20"},
        closing_auction={"enabled": True, "time": "14:57"},
    )
    assert effective_triggers("", "gm", settings) == []
    triggers = effective_triggers("30 9 * * * | 15 9 * * *", "gm", settings)
    times = sorted(trigger.get_next_fire_time(None, moment("09:00")).strftime("%H:%M") for trigger in triggers)
    assert times == ["09:15", "09:20", "09:30", "14:57"]
    assert len(effective_triggers("30 9 * * *", "ctp")) == 1


@pytest.mark.parametrize(
    ("phase", "time"),
    [
        ("opening_cancelable", "09:19"),
        ("opening_cancelable", "09:20"),
        ("opening_locked", "09:19"),
        ("opening_locked", "09:25"),
        ("closing_auction", "14:59"),
        ("closing_auction", "15:00"),
        ("after_hours", "15:30"),
        ("opening_locked", "9:20"),
        ("opening_locked", "09:99"),
    ],
)
def test_settings_reject_nonfixed_times(phase, time):
    with pytest.raises(ValueError, match="触发时间"):
        GMSessionSettings(**{phase: {"enabled": False, "time": time}})


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("minute", range(20, 25))
def test_settings_accept_opening_locked_trigger_range(enabled, minute):
    clock = f"09:{minute}"
    settings = GMSessionSettings(opening_locked={"enabled": enabled, "time": clock})
    assert settings.opening_locked.enabled is enabled
    assert settings.opening_locked.time == clock


@pytest.mark.parametrize(
    ("model", "values", "field"),
    [(GMSessionTime, {"time": "09:20"}, "time"), (GMSessionSettings, {}, "opening_locked")],
)
def test_settings_forbid_extra_fields_and_mutation(model, values, field):
    with pytest.raises(ValueError, match="extra_forbidden"):
        model(**values, unknown=True)
    settings = model(**values)
    with pytest.raises(ValueError, match="frozen_instance"):
        setattr(settings, field, getattr(settings, field))


def test_after_hours_is_unavailable_on_server_and_settings_are_gm_only():
    with pytest.raises(ValueError, match="暂不能启用"):
        GMSessionSettings(after_hours={"enabled": True, "time": "15:05"})
    with pytest.raises(ValueError, match="仅适用于"):
        GMSessionSettings().validate_channel("ctp")
