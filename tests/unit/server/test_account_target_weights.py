"""账户级「执行器口径」目标权重接口的路由测试。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axile.common.trade_channel import TradeChannel
from axile.domain.execution import ExecutionArtifactType
from axile.server.api.deps import get_db
from axile.server.api.routes import account as account_routes
from axile.server.api.routes import account_execution as account_execution_routes
from axile.server.db.models import Account, ExecutionArtifact, Portfolio
from axile.server.target_weight_snapshots import TargetWeightSnapshotData


class _RecordHeaderResult:
    def __init__(self, row: SimpleNamespace | None) -> None:
        self._row = row

    def first(self) -> SimpleNamespace | None:
        return self._row


class _RouteSession:
    """支持按模型区分 ``get(Account, id)`` 与 ``get(Portfolio, id)`` 的极简会话。"""

    async def close(self) -> None:
        return None

    def __init__(
        self,
        account: Account | None,
        portfolio: Portfolio | None,
        artifacts: list[ExecutionArtifact] | None = None,
    ) -> None:
        self._account = account
        self._portfolio = portfolio
        self._artifacts = artifacts or []
        self._execution_records = {
            artifact.execution_id: SimpleNamespace(
                id=index + 1,
                input_id=f"input:{artifact.execution_id}",
            )
            for index, artifact in enumerate(self._artifacts)
            if artifact.artifact_type == ExecutionArtifactType.EXECUTION_SUMMARY
        }

    async def get(self, model: object, obj_id: int) -> object | None:
        if model is Account:
            if self._account is not None and self._account.id == obj_id:
                return self._account
            return None
        if model is Portfolio:
            if self._portfolio is not None and self._portfolio.id == obj_id:
                return self._portfolio
            return None
        return None

    async def execute(self, statement: Any) -> _RecordHeaderResult:
        """仅接受执行 ID、账户过滤后的主键和输入引用查询。"""
        assert [column.name for column in statement.selected_columns] == ["id", "input_id"]
        parameters = statement.compile().params
        account_id = parameters["account_id_1"]
        assert self._account is not None and account_id == self._account.id
        return _RecordHeaderResult(self._execution_records.get(parameters["execution_id_1"]))


@pytest.fixture(autouse=True)
def _typed_sizing_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """将用例中的冻结证据作为窄事实桩；生产路由不读取旧附件正文。"""
    sessions_by_input: dict[str, _RouteSession] = {}

    async def artifact_versions(session: _RouteSession, execution_id: str) -> dict[str, int]:
        sessions_by_input[f"input:{execution_id}"] = session
        return {
            artifact.artifact_type: artifact.schema_version
            for artifact in session._artifacts
            if artifact.execution_id == execution_id
        }

    async def input_id(session: _RouteSession, *, account_id: int, execution_id: str) -> str | None:
        assert session._account is not None and account_id == session._account.id
        if any(
            artifact.execution_id == execution_id and artifact.artifact_type == ExecutionArtifactType.TARGET_SNAPSHOT
            for artifact in session._artifacts
        ):
            return f"input:{execution_id}"
        return None

    async def artifact_input_id(session: _RouteSession, *, account_id: int, execution_id: str, view: str) -> str | None:
        assert view == "targets"
        return await input_id(session, account_id=account_id, execution_id=execution_id)

    async def sizing_context(session: _RouteSession, selected_input_id: str) -> tuple[bool, object]:
        assert sessions_by_input[selected_input_id] is session
        execution_id = selected_input_id.removeprefix("input:")
        for artifact in session._artifacts:
            if (
                artifact.execution_id == execution_id
                and artifact.artifact_type == ExecutionArtifactType.TARGET_SNAPSHOT
            ):
                return "sizing_context" in artifact.content, artifact.content.get("sizing_context")
        return False, None

    async def sizing_rows(session: _RouteSession, record_id: int) -> dict[str, dict[str, object]]:
        execution_id = next(
            execution_id for execution_id, record in session._execution_records.items() if record.id == record_id
        )
        summary = next(
            artifact
            for artifact in session._artifacts
            if artifact.execution_id == execution_id
            and artifact.artifact_type == ExecutionArtifactType.EXECUTION_SUMMARY
        )
        return {
            item["symbol"]: dict(item["sizing"])
            for item in summary.content.get("reconciliation", {}).get("symbols", [])
            if isinstance(item.get("sizing"), dict)
        }

    monkeypatch.setattr(account_execution_routes, "_load_execution_artifact_versions", artifact_versions)
    monkeypatch.setattr(account_execution_routes, "find_input_id", input_id)
    monkeypatch.setattr(account_execution_routes, "read_artifact_input_id", artifact_input_id)
    monkeypatch.setattr(account_execution_routes, "load_sizing_context", sizing_context)
    monkeypatch.setattr(account_execution_routes, "read_sizing_rows", sizing_rows)


def _build_account(
    *,
    long_leverage: float | None = 3.0,
    short_leverage: float | None = 3.0,
    weight_precision: float = 0.01,
) -> Account:
    return Account(
        id=1,
        name="ctp-testnet-sim",
        market="期货",
        trade_channel=TradeChannel.CTP,
        account_config={"api_key": "k", "secret_key": "s", "is_testnet": True},
        is_started=True,
        cron_expr="3,6,9 * * * *",
        remark=None,
        brokerage="ctp",
        weight_precision=weight_precision,
        long_leverage=long_leverage,
        short_leverage=short_leverage,
        algorithm={"method": "SINGLE-MAKER", "params": {}},
        trade_rules={},
        forbidden_symbols=[],
        risk_symbols=[],
        feishu_key=None,
        portfolio_id=7,
        write_empty_record=0,
    )


def _build_portfolio() -> Portfolio:
    return Portfolio(
        id=7,
        name="combo",
        market="期货",
        description="desc",
        custom_calc_py_code="def calculate_portfolio(context):\n    return {}\n",
        status="active",
        tag="core",
    )


def _build_app(session: _RouteSession) -> FastAPI:
    app = FastAPI()
    app.include_router(account_routes.router)

    async def _override_get_db() -> AsyncGenerator[_RouteSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


def _patch_resolution(
    monkeypatch,
    *,
    portfolio_id: int | None,
    raw_target: dict[str, float],
) -> list[TargetWeightSnapshotData]:
    """桩掉绑定组合解析与裸目标合成，把测试聚焦在杠杆缩放这一层。"""

    async def fake_get_latest_portfolio_id_by_account_id(_session: object, _account_id: int) -> int | None:
        return portfolio_id

    async def fake_resolve_portfolio_target(_portfolio: Portfolio, _account: object) -> dict[str, float]:
        return dict(raw_target)

    saved: list[TargetWeightSnapshotData] = []

    async def fake_append_target_weight_snapshot(_session: object, **kwargs: object) -> TargetWeightSnapshotData:
        snapshot = TargetWeightSnapshotData(id=1, execution_id=None, calculated_at="2026-08-25T10:30:00", **kwargs)
        saved.append(snapshot)
        return snapshot

    monkeypatch.setattr(
        account_execution_routes,
        "get_latest_portfolio_id_by_account_id",
        fake_get_latest_portfolio_id_by_account_id,
    )
    monkeypatch.setattr(account_execution_routes, "resolve_portfolio_target", fake_resolve_portfolio_target)
    monkeypatch.setattr(
        account_execution_routes,
        "append_target_weight_snapshot",
        fake_append_target_weight_snapshot,
    )

    async def fake_previous(*_args: object, **_kwargs: object) -> list[TargetWeightSnapshotData]:
        return []

    monkeypatch.setattr(account_execution_routes, "get_previous_account_target_snapshots", fake_previous)

    return saved


def test_account_target_weights_applies_long_leverage(monkeypatch) -> None:
    """多头目标应按 ``long_leverage`` 放大：策略净 50% × 3x → 展示 150%。"""
    session = _RouteSession(_build_account(long_leverage=3.0), _build_portfolio())
    saved = _patch_resolution(monkeypatch, portfolio_id=7, raw_target={"rb2610": 0.5, "ag2612": 0.5})

    response = TestClient(_build_app(session)).post("/account/1/target_snapshot/refresh")

    assert response.status_code == 200
    assert response.json()["weights"] == {"rb2610": 1.5, "ag2612": 1.5}
    assert response.json()["quantities"] is None
    assert response.json()["sizing"]["status"] == "pending_execution"
    assert saved[0].source == "manual"


def test_account_target_weights_applies_short_leverage(monkeypatch) -> None:
    """空头目标（负权重）按 ``short_leverage`` 放大，多空各用各的乘数。"""
    session = _RouteSession(_build_account(long_leverage=1.0, short_leverage=2.0), _build_portfolio())
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={"rb2610": 0.5, "ag2612": -0.5})

    response = TestClient(_build_app(session)).post("/account/1/target_snapshot/refresh")

    assert response.status_code == 200
    assert response.json()["weights"] == {"rb2610": 0.5, "ag2612": -1.0}


def test_account_target_weights_falls_back_to_channel_default_leverage(monkeypatch) -> None:
    """杠杆为 ``None`` 时回落到渠道默认（CTP 多头默认 3x）。"""
    session = _RouteSession(_build_account(long_leverage=None, short_leverage=None), _build_portfolio())
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={"rb2610": 0.5})

    response = TestClient(_build_app(session)).post("/account/1/target_snapshot/refresh")

    assert response.status_code == 200
    assert response.json()["weights"] == {"rb2610": 1.5}


def test_account_target_snapshot_get_only_reads_snapshot(monkeypatch) -> None:
    """快照 GET 只读既有快照，不执行组合函数。"""
    session = _RouteSession(_build_account(), _build_portfolio())
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={"should_not_run": 1.0})

    async def fake_latest(_session: object, _account_id: int, _portfolio_id: int) -> TargetWeightSnapshotData:
        return TargetWeightSnapshotData(
            id=2,
            portfolio_id=7,
            account_id=1,
            raw_weights={"rb2610": 0.5},
            normalized_weights={"rb2610": 1.5},
            source="execution",
            execution_id="exec-1",
            calculated_at="2026-08-25T10:00:00",
        )

    monkeypatch.setattr(account_execution_routes, "get_latest_account_target_snapshot", fake_latest)
    response = TestClient(_build_app(session)).get("/account/1/target_snapshot")

    assert response.status_code == 200
    assert response.json()["weights"] == {"rb2610": 1.5}
    assert response.json()["source"] == "execution"
    assert response.json()["quantities"] is None
    assert response.json()["sizing"]["status"] == "legacy"


def test_account_target_snapshot_returns_uncalculated_without_bound_portfolio(monkeypatch) -> None:
    """账户未绑定组合时返回结构化未计算态。"""
    session = _RouteSession(_build_account(), _build_portfolio())
    _patch_resolution(monkeypatch, portfolio_id=None, raw_target={"rb2610": 0.5})

    response = TestClient(_build_app(session)).get("/account/1/target_snapshot")

    assert response.status_code == 200
    assert response.json()["weights"] == {}
    assert response.json()["calculated_at"] is None


def test_account_target_snapshot_returns_404_for_unknown_account(monkeypatch) -> None:
    """账户不存在时返回 404。"""
    session = _RouteSession(None, _build_portfolio())
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={"rb2610": 0.5})

    response = TestClient(_build_app(session)).get("/account/999/target_snapshot")

    assert response.status_code == 404


def test_account_target_refresh_rejects_operation_conflict(monkeypatch) -> None:
    session = _RouteSession(_build_account(), _build_portfolio())
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={"rb2610": 0.5})
    monkeypatch.setattr(account_execution_routes, "try_register_target_refresh", lambda *_args: False)

    response = TestClient(_build_app(session)).post("/account/1/target_snapshot/refresh")

    assert response.status_code == 409


def test_account_target_snapshot_does_not_recalculate_legacy_quantity_from_current_book(monkeypatch) -> None:
    """历史执行缺少当时证据时只规范 symbol，不拿当前账面和行情反推数量."""
    session = _RouteSession(_build_account(), _build_portfolio())
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={"TA2701": -0.08})

    async def fake_latest(_session: object, _account_id: int, _portfolio_id: int) -> TargetWeightSnapshotData:
        return TargetWeightSnapshotData(
            id=3,
            portfolio_id=7,
            account_id=1,
            raw_weights={"TA2701": -0.0267},
            normalized_weights={"TA2701": -0.08},
            source="execution",
            execution_id="exec-lot",
            calculated_at="2026-08-26T21:35:10",
        )

    monkeypatch.setattr(account_execution_routes, "get_latest_account_target_snapshot", fake_latest)

    response = TestClient(_build_app(session)).get("/account/1/target_snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["weights"] == {"TA701": -0.08}
    assert body["quantities"] is None
    assert body["sizing"]["status"] == "legacy"


def test_account_target_snapshot_returns_complete_execution_sizing(monkeypatch) -> None:
    """仅同一次 v2 执行的完整逐只证据可形成 quantities."""
    artifacts = [
        ExecutionArtifact(
            execution_id="exec-v2",
            artifact_type=ExecutionArtifactType.TARGET_SNAPSHOT,
            schema_version=2,
            content={"sizing_context": {"weight_precision": 0.01}},
        ),
        ExecutionArtifact(
            execution_id="exec-v2",
            artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
            schema_version=2,
            content={
                "reconciliation": {
                    "symbols": [
                        {
                            "symbol": "TA701",
                            "sizing": {
                                "symbol": "TA701",
                                "reason_code": "COMMON.SIZING.QUANTIZED",
                                "account_weight": -0.32,
                                "equity": 99_974.0,
                                "reference_price": 4_840.0,
                                "unit_multiplier": 5.0,
                                "unit_notional": 24_200.0,
                                "raw_quantity": -1.322,
                                "target_quantity": -1.0,
                                "quantity_step": 1.0,
                            },
                        }
                    ]
                }
            },
        ),
    ]
    session = _RouteSession(_build_account(), _build_portfolio(), artifacts)
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={})

    async def fake_latest(_session: object, _account_id: int, _portfolio_id: int) -> TargetWeightSnapshotData:
        return TargetWeightSnapshotData(
            id=4,
            portfolio_id=7,
            account_id=1,
            raw_weights={"TA2701": -0.1067},
            normalized_weights={"TA2701": -0.32},
            source="execution",
            execution_id="exec-v2",
            calculated_at="2026-08-27T13:30:00",
        )

    monkeypatch.setattr(account_execution_routes, "get_latest_account_target_snapshot", fake_latest)
    response = TestClient(_build_app(session)).get("/account/1/target_snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["strategy_weights"] == {"TA701": -0.1067}
    assert body["account_weights"] == {"TA701": -0.32}
    assert body["quantities"] == {"TA701": -1.0}
    assert body["sizing"]["status"] == "available"
    assert body["sizing"]["rows"]["TA701"]["account_multiplier"] == pytest.approx(2.99906279)
    assert body["sizing"]["rows"]["TA701"]["weight_precision"] == 0.01


@pytest.mark.parametrize(
    ("artifacts", "expected_status"),
    [
        (
            [
                ExecutionArtifact(
                    execution_id="exec-status",
                    artifact_type=ExecutionArtifactType.TARGET_SNAPSHOT,
                    schema_version=2,
                    content={"sizing_context": {"weight_precision": 0.01}},
                )
            ],
            "pending_execution",
        ),
        (
            [
                ExecutionArtifact(
                    execution_id="exec-status",
                    artifact_type=ExecutionArtifactType.TARGET_SNAPSHOT,
                    schema_version=2,
                    content={"sizing_context": {"weight_precision": 0.01}},
                ),
                ExecutionArtifact(
                    execution_id="exec-status",
                    artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
                    schema_version=2,
                    content={"reconciliation": {"symbols": []}},
                ),
            ],
            "pending_execution",
        ),
        (
            [
                ExecutionArtifact(
                    execution_id="exec-status",
                    artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
                    schema_version=1,
                    content={"reconciliation": {"symbols": []}},
                )
            ],
            "legacy",
        ),
    ],
    ids=["v2-target-pending", "v2-summary-incomplete", "v1-summary-legacy"],
)
def test_account_target_snapshot_reports_sizing_evidence_state(
    monkeypatch,
    artifacts: list[ExecutionArtifact],
    expected_status: str,
) -> None:
    """API 应区分执行尚未完成、证据不完整与旧协议记录."""
    session = _RouteSession(_build_account(), _build_portfolio(), artifacts)
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={})

    async def fake_latest(_session: object, _account_id: int, _portfolio_id: int) -> TargetWeightSnapshotData:
        return TargetWeightSnapshotData(
            id=5,
            portfolio_id=7,
            account_id=1,
            raw_weights={"rb2610": 0.1},
            normalized_weights={"rb2610": 0.3},
            source="execution",
            execution_id="exec-status",
            calculated_at="2026-08-27T14:00:00",
        )

    monkeypatch.setattr(account_execution_routes, "get_latest_account_target_snapshot", fake_latest)

    response = TestClient(_build_app(session)).get("/account/1/target_snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["sizing"]["status"] == expected_status
    assert body["quantities"] is None


def _complete_sizing_artifacts(
    execution_id: str, *, symbol: str = "TA701", quantity: float = -1.0
) -> list[ExecutionArtifact]:
    return [
        ExecutionArtifact(
            execution_id=execution_id,
            artifact_type=ExecutionArtifactType.TARGET_SNAPSHOT,
            schema_version=2,
            content={"sizing_context": {"weight_precision": 0.01}},
        ),
        ExecutionArtifact(
            execution_id=execution_id,
            artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
            schema_version=2,
            content={
                "reconciliation": {
                    "symbols": [
                        {
                            "symbol": symbol,
                            "sizing": {
                                "symbol": symbol,
                                "reason_code": "COMMON.SIZING.QUANTIZED",
                                "account_weight": -0.32,
                                "target_quantity": quantity,
                                "quantity_step": 1.0,
                            },
                        }
                    ]
                }
            },
        ),
    ]


def _empty_summary_artifacts(execution_id: str) -> list[ExecutionArtifact]:
    return [
        ExecutionArtifact(
            execution_id=execution_id,
            artifact_type=ExecutionArtifactType.TARGET_SNAPSHOT,
            schema_version=2,
            content={"sizing_context": {"weight_precision": 0.01}},
        ),
        ExecutionArtifact(
            execution_id=execution_id,
            artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
            schema_version=2,
            content={"reconciliation": {"symbols": []}},
        ),
    ]


def _unavailable_sizing_artifacts(execution_id: str, *, symbol: str = "TA701") -> list[ExecutionArtifact]:
    return [
        ExecutionArtifact(
            execution_id=execution_id,
            artifact_type=ExecutionArtifactType.TARGET_SNAPSHOT,
            schema_version=2,
            content={"sizing_context": {"weight_precision": 0.01}},
        ),
        ExecutionArtifact(
            execution_id=execution_id,
            artifact_type=ExecutionArtifactType.EXECUTION_SUMMARY,
            schema_version=2,
            content={
                "reconciliation": {
                    "symbols": [
                        {
                            "symbol": symbol,
                            "sizing": {
                                "symbol": symbol,
                                "reason_code": "COMMON.SIZING.MISSING_MARKET_DATA",
                                "account_weight": -0.32,
                                "target_quantity": None,
                            },
                        }
                    ]
                }
            },
        ),
    ]


def test_account_target_snapshot_falls_back_to_same_weight_complete_sizing(monkeypatch) -> None:
    """最新 BLOCKED 没手数时，沿用同一套权重上一次完整换算."""
    artifacts = _empty_summary_artifacts("exec-blocked") + _complete_sizing_artifacts("exec-sized")
    session = _RouteSession(_build_account(), _build_portfolio(), artifacts)
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={})

    async def fake_latest(_session: object, _account_id: int, _portfolio_id: int) -> TargetWeightSnapshotData:
        return TargetWeightSnapshotData(
            id=20,
            portfolio_id=7,
            account_id=1,
            raw_weights={"TA2701": -0.1067},
            normalized_weights={"TA2701": -0.32},
            source="execution",
            execution_id="exec-blocked",
            calculated_at="2026-09-17T11:34:00",
        )

    async def fake_previous(*_args: object, **_kwargs: object) -> list[TargetWeightSnapshotData]:
        return [
            TargetWeightSnapshotData(
                id=19,
                portfolio_id=7,
                account_id=1,
                raw_weights={"TA2701": -0.1067},
                normalized_weights={"TA2701": -0.32},
                source="execution",
                execution_id="exec-sized",
                calculated_at="2026-09-17T11:19:00",
            )
        ]

    monkeypatch.setattr(account_execution_routes, "get_latest_account_target_snapshot", fake_latest)
    monkeypatch.setattr(account_execution_routes, "get_previous_account_target_snapshots", fake_previous)

    body = TestClient(_build_app(session)).get("/account/1/target_snapshot").json()
    assert body["weights"] == {"TA701": -0.32}
    assert body["quantities"] == {"TA701": -1.0}
    assert body["sizing"]["status"] == "available"
    assert body["sizing"]["execution_id"] == "exec-sized"
    assert body["sizing"]["calculated_at"] == "2026-09-17T11:19:00"
    assert body["execution_id"] == "exec-blocked"
    assert body["calculated_at"] == "2026-09-17T11:34:00"


def test_account_target_snapshot_does_not_reuse_lots_when_weights_changed(monkeypatch) -> None:
    artifacts = _empty_summary_artifacts("exec-blocked") + _complete_sizing_artifacts("exec-sized")
    session = _RouteSession(_build_account(), _build_portfolio(), artifacts)
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={})

    async def fake_latest(_session: object, _account_id: int, _portfolio_id: int) -> TargetWeightSnapshotData:
        return TargetWeightSnapshotData(
            id=20,
            portfolio_id=7,
            account_id=1,
            raw_weights={"TA2701": -0.2},
            normalized_weights={"TA2701": -0.6},
            source="execution",
            execution_id="exec-blocked",
            calculated_at="2026-09-17T11:34:00",
        )

    async def fake_previous(*_args: object, **_kwargs: object) -> list[TargetWeightSnapshotData]:
        return [
            TargetWeightSnapshotData(
                id=19,
                portfolio_id=7,
                account_id=1,
                raw_weights={"TA2701": -0.1067},
                normalized_weights={"TA2701": -0.32},
                source="execution",
                execution_id="exec-sized",
                calculated_at="2026-09-17T11:19:00",
            )
        ]

    monkeypatch.setattr(account_execution_routes, "get_latest_account_target_snapshot", fake_latest)
    monkeypatch.setattr(account_execution_routes, "get_previous_account_target_snapshots", fake_previous)

    body = TestClient(_build_app(session)).get("/account/1/target_snapshot").json()
    assert body["quantities"] is None
    assert body["sizing"]["status"] == "pending_execution"
    assert body["sizing"]["execution_id"] == "exec-blocked"


def test_account_target_snapshot_falls_back_when_current_quantities_are_null(monkeypatch) -> None:
    artifacts = _unavailable_sizing_artifacts("exec-now") + _complete_sizing_artifacts("exec-sized")
    session = _RouteSession(_build_account(), _build_portfolio(), artifacts)
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={})

    async def fake_latest(_session: object, _account_id: int, _portfolio_id: int) -> TargetWeightSnapshotData:
        return TargetWeightSnapshotData(
            id=20,
            portfolio_id=7,
            account_id=1,
            raw_weights={"TA2701": -0.1067},
            normalized_weights={"TA2701": -0.32},
            source="execution",
            execution_id="exec-now",
            calculated_at="2026-09-17T11:34:00",
        )

    async def fake_previous(*_args: object, **_kwargs: object) -> list[TargetWeightSnapshotData]:
        return [
            TargetWeightSnapshotData(
                id=19,
                portfolio_id=7,
                account_id=1,
                raw_weights={"TA2701": -0.1067},
                normalized_weights={"TA2701": -0.32},
                source="execution",
                execution_id="exec-sized",
                calculated_at="2026-09-17T11:19:00",
            )
        ]

    monkeypatch.setattr(account_execution_routes, "get_latest_account_target_snapshot", fake_latest)
    monkeypatch.setattr(account_execution_routes, "get_previous_account_target_snapshots", fake_previous)

    body = TestClient(_build_app(session)).get("/account/1/target_snapshot").json()
    assert body["quantities"] == {"TA701": -1.0}
    assert body["sizing"]["status"] == "available"
    assert body["sizing"]["execution_id"] == "exec-sized"


def test_account_target_snapshot_keeps_unavailable_without_complete_history(monkeypatch) -> None:
    session = _RouteSession(_build_account(), _build_portfolio(), _unavailable_sizing_artifacts("exec-now"))
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={})

    async def fake_latest(_session: object, _account_id: int, _portfolio_id: int) -> TargetWeightSnapshotData:
        return TargetWeightSnapshotData(
            id=20,
            portfolio_id=7,
            account_id=1,
            raw_weights={"TA2701": -0.1067},
            normalized_weights={"TA2701": -0.32},
            source="execution",
            execution_id="exec-now",
            calculated_at="2026-09-17T11:34:00",
        )

    monkeypatch.setattr(account_execution_routes, "get_latest_account_target_snapshot", fake_latest)

    body = TestClient(_build_app(session)).get("/account/1/target_snapshot").json()
    assert body["quantities"] is None
    assert body["sizing"]["status"] == "unavailable"


def test_account_target_snapshot_does_not_union_partial_histories(monkeypatch) -> None:
    artifacts = (
        _empty_summary_artifacts("exec-now")
        + _complete_sizing_artifacts("exec-a", symbol="TA701", quantity=-1.0)
        + _complete_sizing_artifacts("exec-b", symbol="rb2610", quantity=2.0)
    )
    session = _RouteSession(_build_account(), _build_portfolio(), artifacts)
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={})

    async def fake_latest(_session: object, _account_id: int, _portfolio_id: int) -> TargetWeightSnapshotData:
        return TargetWeightSnapshotData(
            id=30,
            portfolio_id=7,
            account_id=1,
            raw_weights={"TA2701": -0.1067, "rb2610": 0.1},
            normalized_weights={"TA2701": -0.32, "rb2610": 0.3},
            source="execution",
            execution_id="exec-now",
            calculated_at="2026-09-17T11:34:00",
        )

    async def fake_previous(*_args: object, **_kwargs: object) -> list[TargetWeightSnapshotData]:
        return [
            TargetWeightSnapshotData(
                id=29,
                portfolio_id=7,
                account_id=1,
                raw_weights={"TA2701": -0.1067},
                normalized_weights={"TA2701": -0.32},
                source="execution",
                execution_id="exec-a",
                calculated_at="2026-09-17T11:19:00",
            ),
            TargetWeightSnapshotData(
                id=28,
                portfolio_id=7,
                account_id=1,
                raw_weights={"rb2610": 0.1},
                normalized_weights={"rb2610": 0.3},
                source="execution",
                execution_id="exec-b",
                calculated_at="2026-09-17T11:18:00",
            ),
        ]

    monkeypatch.setattr(account_execution_routes, "get_latest_account_target_snapshot", fake_latest)
    monkeypatch.setattr(account_execution_routes, "get_previous_account_target_snapshots", fake_previous)

    body = TestClient(_build_app(session)).get("/account/1/target_snapshot").json()
    assert body["quantities"] is None
    assert body["sizing"]["status"] == "pending_execution"


def test_account_target_refresh_reuses_same_weight_lots(monkeypatch) -> None:
    artifacts = _complete_sizing_artifacts("exec-sized")
    session = _RouteSession(_build_account(), _build_portfolio(), artifacts)
    _patch_resolution(monkeypatch, portfolio_id=7, raw_target={"TA2701": -0.1067})

    async def fake_previous(*_args: object, **_kwargs: object) -> list[TargetWeightSnapshotData]:
        return [
            TargetWeightSnapshotData(
                id=19,
                portfolio_id=7,
                account_id=1,
                raw_weights={"TA2701": -0.1067},
                normalized_weights={"TA2701": -0.32},
                source="execution",
                execution_id="exec-sized",
                calculated_at="2026-09-17T11:19:00",
            )
        ]

    monkeypatch.setattr(account_execution_routes, "get_previous_account_target_snapshots", fake_previous)

    body = TestClient(_build_app(session)).post("/account/1/target_snapshot/refresh").json()
    assert body["source"] == "manual"
    assert body["quantities"] == {"TA701": -1.0}
    assert body["sizing"]["status"] == "available"
    assert body["sizing"]["execution_id"] == "exec-sized"
