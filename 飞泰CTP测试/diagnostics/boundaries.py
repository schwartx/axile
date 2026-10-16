"""#110/#111/#112 核心边界表征：目录来源和 worker IPC 均为桩，不访问柜台。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from axile.executor.execution_engine import ExecutionEngine
from axile.executor.instrument_catalog import CatalogError, InstrumentCatalog
from axile.executor.models.unified_account_assets import UnifiedAccountAssets
from axile.server.evidence.collect import _select_execution
from axile.server.evidence.models import (
    EvidenceError,
    EvidenceLimits,
    EvidenceSnapshot,
    ExecutionEvidence,
    ExportBudget,
)
from axile.server.evidence.report import _coverage
from axile.server.execution.worker_backend.manager import WorkerBackendManager
from axile.server.execution.worker_backend.protocol import WorkerBackendResponse
from axile.server.portfolio_function import PortfolioFunctionError, PortfolioFunctionResult


def main():
    catalog = InstrumentCatalog()
    catalog.register("test", lambda *_args, **_kwargs: [], channel="ctp")
    try:
        catalog.ensure(["FT_UNKNOWN_CONTRACT"], require_specs=True)
        raise AssertionError("empty source was accepted")
    except CatalogError as error:
        print(
            json.dumps(
                {"probe": "catalog negative lookup", "reason_code": error.reason_code, "detail": str(error)},
                ensure_ascii=False,
            )
        )
        assert error.reason_code == "CATALOG.UNAVAILABLE"
    finally:
        catalog.close()

    async def portfolio_drop():
        manager = WorkerBackendManager()
        account = SimpleNamespace(id=1, model_dump=lambda **_: {"id": 1})
        result = PortfolioFunctionResult(
            ok=False, error=PortfolioFunctionError("unknown contract", error_type="CatalogError")
        )

        async def response(_func, *args, **_kwargs):
            return WorkerBackendResponse(
                request_id=args[1].request_id, kind="result", output_payload=result.to_payload()
            )

        dropped = []

        async def drop(account_id):
            dropped.append(account_id)

        with (
            patch.object(manager, "_check_read_session"),
            patch.object(manager, "drop_account", side_effect=drop),
            patch("asyncio.to_thread", side_effect=response),
        ):
            outcome = await manager.calculate_portfolio(account, "unused code")
        manager.close()
        assert not outcome.ok and dropped == [1]
        print(
            json.dumps(
                {"probe": "typed CatalogError in portfolio result", "dropped_worker_accounts": dropped},
                ensure_ascii=False,
            )
        )

    asyncio.run(portfolio_drop())

    blocked = []

    def read_quotes(symbols):
        blocked.extend(symbols)
        raise TimeoutError("stale quote for flat zero-target rb2701")

    owner = SimpleNamespace(get_market_data=read_quotes, calculate_target_sizing=Mock())
    engine = ExecutionEngine(owner, runtime=SimpleNamespace())
    standard_input = SimpleNamespace(
        last_target={"rb2701": 1},
        trade_rules={"T2612": {"sizing_mode": "lots"}, "rb2701": {"sizing_mode": "lots"}},
        forbidden_symbols=[],
    )
    assets = UnifiedAccountAssets(available_cash=1000, total_asset=1000, market_value=0, positions=[], source="real")
    try:
        engine._plan_target_volumes_for_symbols(
            standard_input=standard_input,
            account_assets=assets,
            symbols=["T2612", "rb2701"],
            effective_curr_target={"T2612": 1, "rb2701": 0},
        )
        raise AssertionError("planning did not request quotes")
    except TimeoutError:
        assert blocked == ["T2612", "rb2701"] and owner.calculate_target_sizing.call_count == 0
        print(
            json.dumps(
                {"probe": "lots planning includes flat zero target", "blocking_quotes": blocked, "sizing_calls": 0},
                ensure_ascii=False,
            )
        )

    required = [
        "environment_snapshot",
        "account_snapshot_before",
        "standard_input",
        "target_snapshot",
        "trade_rules_snapshot",
        "execution_summary",
        "account_snapshot",
    ]
    item = ExecutionEvidence(
        key="known",
        execution_id="known",
        intent={"kind": "rebalance"},
        record={},
        events=[{}],
        artifacts=[{"artifact_type": kind, "content": {}} for kind in required],
        selection_reasons=["explicit_execution"],
    )
    a = _coverage(item, "SUCCEEDED")
    item.selection_reasons.append("counter_bucket_context_time_assumed")
    b = _coverage(item, "SUCCEEDED")
    assert a["coverage"] == "available" and b["issues"] == ["control_counter_activity_time_unknown"]
    print(
        json.dumps(
            {
                "probe": "coverage only selection reason changes",
                "explicit": a["coverage"],
                "date_with_bucket": b["coverage"],
                "issues": b["issues"],
            },
            ensure_ascii=False,
        )
    )
    snapshot = EvidenceSnapshot(account={"id": 1}, executions={})
    budget = ExportBudget()
    for index in range(EvidenceLimits().executions):
        _select_execution(snapshot, str(index), str(index), "explicit_execution", budget)
    try:
        _select_execution(snapshot, "overflow", "overflow", "explicit_execution", budget)
        raise AssertionError("export cap did not apply")
    except EvidenceError as error:
        assert error.code == "EVIDENCE_TOO_MANY_EXECUTIONS"
        print(
            json.dumps(
                {"probe": "account export cap", "limit": EvidenceLimits().executions, "error": error.code},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
