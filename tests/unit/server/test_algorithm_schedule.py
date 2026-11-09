"""分时段算法的输入边界与冻结语义。"""

from datetime import datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from axile.server.db.models.account import AccountUpdate
from axile.server.db.models.algorithm_schedule import AlgorithmSchedule
from axile.server.execution import execution_algorithms
from tests.unit.server._execution_test_support import build_account

SCHEDULE = TypeAdapter(AlgorithmSchedule)


def rule(start="09:30", end="10:00", **params):
    return {"start": start, "end": end, "algorithm": {"method": "TWAP", "params": params}}


@pytest.mark.parametrize(
    "rules",
    [
        [rule("9:30")],
        [rule("09:3١")],
        [rule(end="24:00")],
        [rule(end="09:30")],
        [rule(), rule("09:45", "11:00")],
        [rule("21:00", "02:30"), rule("01:00", "03:00")],
        [rule(slices=0)],
    ],
)
def test_invalid_schedule_is_rejected(rules):
    with pytest.raises(ValidationError):
        AccountUpdate(algorithm_schedule=rules)


def test_midnight_adjacency_and_old_patch_compatibility():
    assert len(SCHEDULE.validate_python([rule("21:00", "00:00"), rule("00:00", "02:30")])) == 2
    assert AccountUpdate(name="changed").model_dump(exclude_unset=True) == {"name": "changed"}
    assert AccountUpdate(algorithm_schedule=[]).model_dump(exclude_unset=True) == {"algorithm_schedule": []}


@pytest.mark.parametrize(
    "moment,expected",
    [
        ("2026-11-09T01:29:59+00:00", None),
        ("2026-11-09T01:30:00+00:00", "09:30"),
        ("2026-11-09T02:00:00+00:00", "10:00"),
        ("2026-11-09T03:00:00+00:00", None),
        ("2026-11-09T21:00:00+08:00", "21:00"),
        ("2026-11-10T02:29:59+08:00", "21:00"),
        ("2026-11-10T02:30:00+08:00", None),
    ],
)
def test_selection_uses_beijing_time_and_freezes_complete_params(monkeypatch, moment, expected):
    account = build_account()
    account.algorithm_schedule = SCHEDULE.validate_python(
        [rule(slices=4), rule("10:00", "11:00", slices=6), rule("21:00", "02:30", slices=8)]
    )
    monkeypatch.setattr(execution_algorithms, "clock_now", lambda **_: datetime.fromisoformat(moment))
    selected, evidence = execution_algorithms.select_rebalance_algorithm(account)
    if expected is None:
        assert selected.algorithm == account.algorithm
    else:
        matched = next(item for item in account.algorithm_schedule if item.start == expected)
        assert selected.algorithm == matched.algorithm
        selected.algorithm["params"]["slices"] = 99
        assert matched.algorithm["params"]["slices"] != 99
    assert evidence["start"] == expected
    assert evidence["selected_at"] == moment
    assert evidence["timezone"] == "Asia/Shanghai"
    assert account.algorithm["method"] != "TWAP"


def test_schedule_checks_algorithm_channel_when_only_channel_changes(monkeypatch):
    from axile.server.db.models import account_validation

    account = build_account()
    account.algorithm_schedule = SCHEDULE.validate_python([rule()])

    def incompatible(config, channel, label):
        raise ValueError("时段算法不适配新渠道")

    monkeypatch.setattr(account_validation, "_check_algorithm_channel_compat", incompatible)
    with pytest.raises(ValueError, match="不适配新渠道"):
        type(account).model_validate(account.model_dump() | {"trade_channel": "gm"})


def test_account_http_roundtrip_preserves_schedule_and_rejects_conflicts():
    from fastapi.testclient import TestClient

    from tests.futures_close_app import app, reset

    reset()
    with TestClient(app) as client:
        assert client.get("/api/v1/account/1").json()["algorithm_schedule"] == []
        saved = client.patch("/api/v1/account/1", json={"algorithm_schedule": [rule()]}).json()
        assert saved["algorithm_schedule"] == [rule()]
        assert client.patch("/api/v1/account/1", json={"remark": "unchanged"}).json()["algorithm_schedule"] == [rule()]
        invalid = client.patch("/api/v1/account/1", json={"algorithm_schedule": [rule(), rule("09:45", "11:00")]})
        assert invalid.status_code == 422
        assert client.get("/api/v1/account/1").json()["algorithm_schedule"] == [rule()]
        assert client.patch("/api/v1/account/1", json={"algorithm_schedule": []}).json()["algorithm_schedule"] == []
