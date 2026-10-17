"""离线业务回归，执行与联机相同的场景算法及结果核对。"""

import copy
import json

import pytest

from ft_ctp.api import ApiError
from ft_ctp.cli import main, run_scenarios
from ft_ctp.evidence import Bundle
from ft_ctp.runner import Runner
from support import HarnessConfig as Config
from support import OfflineApi


def make_runner(tmp_path, **overrides):
    values = {"results_dir": str(tmp_path), "timeout_seconds": 2, "poll_seconds": 0.2, **overrides}
    config = Config(**values)
    config.validate()
    api = OfflineApi(config)
    bundle = Bundle(str(tmp_path), "case", config.public())
    return Runner(config, api, bundle, 4), api


def test_trade_cycle_and_idempotent_target(tmp_path):
    runner, api = make_runner(tmp_path, max_requests=20)
    original = copy.deepcopy(api.account)
    run_scenarios(runner, ["open", "open", "close", "cancel", "stats"])
    assert all(row["status"] == "passed" for row in runner.bundle.index["scenarios"])
    assert api.counts == {"place_order": 6, "cancel_order": 4, "cancel_order_ctp": 4}
    assert api.volume == 0
    assert api.account == original
    assert runner.bundle.index["missing"] == []
    second = json.loads((runner.bundle.path / "02-open/artifacts.json").read_text())
    assert second["data"][0]["content"]["results"][0]["orders"] == []
    assert (runner.bundle.path / "manifest.json").exists()


def test_unrelated_positions_are_preserved_as_same_lots_targets(tmp_path):
    from axile.executor.models.unified_account_assets import Position

    runner, api = make_runner(tmp_path)
    position = Position(symbol="TL2612", volume=16, available_volume=16, market_value=1600000, direction="多头")
    api.extra_positions = [position]
    run_scenarios(runner, ["open", "close"])
    assert all(row["status"] == "passed" for row in runner.bundle.index["scenarios"])
    assert api.extra_positions[0].volume == 16
    assert runner.preserved_targets == {"TL2612": 16}
    assert "'TL2612': 16" in api.portfolio["custom_calc_py_code"]
    api.extra_positions = [position.model_copy(update={"volume": 15})]
    with pytest.raises(Exception, match="无关持仓数量发生变化"):
        runner.check_preserved(api.assets().model_dump(mode="json"))


@pytest.mark.parametrize("invalid_case", ["price", "volume", "unknown"])
def test_invalid_inputs_hit_specific_validation_without_native_insert(tmp_path, invalid_case):
    runner, api = make_runner(tmp_path, invalid_case=invalid_case)
    run_scenarios(runner, ["invalid"])
    assert runner.bundle.index["scenarios"][0]["status"] == "passed"
    assert sum(s.native_calls for s in api.sessions.values()) == 0


@pytest.mark.parametrize("side", ["long", "short"])
def test_direction_and_close_today(tmp_path, side):
    runner, api = make_runner(tmp_path, side=side, close_offset="today")
    run_scenarios(runner, ["open", "close"])
    assert all(row["status"] == "passed" for row in runner.bundle.index["scenarios"])
    closed = api.sessions["offline-0002"]
    assert next(iter(closed.orders.values())).extra["offset_flag"] == "close_today"
    assert api.volume == 0


def test_close_yesterday_does_not_close_today_instead(tmp_path):
    runner, api = make_runner(tmp_path, close_offset="yesterday")
    run_scenarios(runner, ["open", "close"])
    assert runner.bundle.index["scenarios"][1]["status"] == "insufficient_conditions"
    assert api.volume == 1
    assert api.sessions["offline-0002"].native_calls == 0


def test_auto_close_splits_today_and_yesterday_and_links_each_fill(tmp_path):
    runner, api = make_runner(
        tmp_path, lots=2, max_order_lots=2, max_exposure_lots=2, offline_initial_lots=2, offline_yesterday_lots=1
    )
    run_scenarios(runner, ["close"])
    assert runner.bundle.index["scenarios"][0]["status"] == "passed"
    orders = list(api.sessions["offline-0001"].orders.values())
    assert [order.extra["offset_flag"] for order in orders] == ["close_yesterday", "close_today"]
    assert len(orders) == 2 and api.volume == 0 and api.yesterday_lots == 0


@pytest.mark.parametrize("fault", ["timeout", "cancel_failed", "sync_reject", "unknown"])
def test_failures_remain_visible_and_stop_following_actions(tmp_path, fault):
    runner, api = make_runner(tmp_path, offline_fault=fault)
    run_scenarios(runner, ["cancel", "open"])
    assert runner.bundle.index["scenarios"][0]["status"] != "passed"
    assert runner.bundle.index["scenarios"][1]["status"] == "not_executed"
    assert len(api.sessions) == 1
    assert (runner.bundle.path / "index.json").exists()
    if fault == "cancel_failed":
        # 撤单失败只调用一次；finally 不能偷偷重试同一请求。
        assert api.counts["cancel_order_ctp"] == 1


def test_partial_fill_is_not_full_target_or_clean_cancel(tmp_path):
    runner, api = make_runner(tmp_path, offline_fault="partial", lots=2, max_order_lots=2, max_exposure_lots=2)
    run_scenarios(runner, ["open", "close"])
    assert runner.bundle.index["scenarios"][0]["status"] == "failed"
    assert runner.bundle.index["scenarios"][1]["status"] == "not_executed"
    assert api.volume == 1
    evidence = json.loads((runner.bundle.path / "01-open/artifacts.json").read_text())
    result = evidence["data"][0]["content"]["results"][0]
    assert result["orders"][0]["filled_volume"] == 1
    assert result["trades"][0]["trade_volume"] == 1


def test_late_active_callback_does_not_erase_terminal_or_trade(tmp_path):
    runner, api = make_runner(tmp_path, offline_fault="late")
    run_scenarios(runner, ["open", "close"])
    assert all(row["status"] == "passed" for row in runner.bundle.index["scenarios"])
    assert api.volume == 0


def test_threshold_proves_three_positions_and_distinguishes_warning_source(tmp_path):
    runner, api = make_runner(tmp_path)
    run_scenarios(runner, ["threshold"])
    assert runner.bundle.index["scenarios"][0]["status"] == "passed"
    for label, expected in [("before", 2), ("at", 3), ("after", 4)]:
        sample = json.loads((runner.bundle.path / f"01-threshold/{label}.json").read_text())
        assert sample["value"] == expected
        assert sample["script_hint"]["source"] == "script"
    warning = json.loads((runner.bundle.path / "01-threshold/product-warnings.json").read_text())
    assert warning["events"]
    assert api.counts["place_order"] == 4


def test_counter_already_over_warning_line_is_not_reset(tmp_path):
    runner, api = make_runner(tmp_path)
    api.counts["place_order"] = 4
    run_scenarios(runner, ["threshold"])
    assert runner.bundle.index["scenarios"][0]["status"] == "insufficient_conditions"
    assert api.counts["place_order"] == 4


def test_reject_requires_approved_condition_and_matching_async_code(tmp_path):
    runner, api = make_runner(tmp_path, broker_condition="离线回归约定条件，不是真实柜台证据", expected_error_id=90001)
    run_scenarios(runner, ["reject"])
    assert runner.bundle.index["scenarios"][0]["status"] == "passed"
    assert runner.bundle.index["mode"] == "connected"
    assert api.volume == 0


def test_reject_without_condition_is_explicitly_pending(tmp_path):
    runner, api = make_runner(tmp_path)
    run_scenarios(runner, ["reject"])
    assert runner.bundle.index["scenarios"][0]["status"] == "insufficient_conditions"
    assert not api.sessions


@pytest.mark.parametrize("point", ["executing", "quota", "queued"])
def test_pause_anchors_and_waits_for_actual_terminal(tmp_path, point):
    runner, api = make_runner(tmp_path, pause_point=point)
    run_scenarios(runner, ["pause"])
    assert runner.bundle.index["scenarios"][0]["status"] == "passed"
    assert all(row["status"] == "TERMINATED" for row in api.executions.values())
    assert api.counts["place_order"] == 0
    assert (runner.bundle.path / "01-pause/observations.json").exists()


def test_submission_timeout_is_not_retried_or_restored_as_if_nothing_happened(tmp_path):
    runner, api = make_runner(tmp_path)
    request = api.request
    triggers = []

    def uncertain(method, path, body=None, **kwargs):
        if path == "/account/execute/4":
            triggers.append(path)
            request(method, path, body, **kwargs)  # 服务端已受理，但响应丢失。
            raise ApiError("响应超时，受理结果未知")
        return request(method, path, body, **kwargs)

    api.request = uncertain
    run_scenarios(runner, ["open", "open"])
    assert len(triggers) == 1
    assert runner.active_execution == "submission-unknown"
    assert runner.bundle.index["scenarios"][0]["status"] == "unknown"
    assert not (runner.bundle.path / "restored.json").exists()


@pytest.mark.parametrize(
    "point,action", [("idle", "terminate"), ("scheduled", "pause_auto"), ("supplement", "cancel_supplement")]
)
def test_idle_scheduled_and_supplement_are_independent_product_actions(tmp_path, point, action):
    runner, api = make_runner(
        tmp_path, pause_point=point, pause_action=action, timeout_seconds=80, observe_seconds=65, poll_seconds=1
    )
    run_scenarios(runner, ["pause"])
    assert runner.bundle.index["scenarios"][0]["status"] == "passed"
    assert api.counts["place_order"] == 0
    assert not api.account["is_started"]
    if point == "supplement":
        assert runner.execution_ids
        assert not api.account.get("has_pending_supplement")


def test_restart_without_real_instance_change_is_not_passed(tmp_path):
    runner, _ = make_runner(tmp_path, pause_point="restart")
    run_scenarios(runner, ["pause"])
    assert runner.bundle.index["scenarios"][0]["status"] == "insufficient_conditions"


def test_hold_does_not_add_to_an_already_reached_target(tmp_path):
    from axile.executor.algorithms.core.base import AlgorithmInput

    from ft_ctp.algorithm import ScenarioParams, scenario_algorithm
    from support import OfflineSession

    runner, api = make_runner(tmp_path, offline_initial_lots=1)
    params = ScenarioParams(symbol="rb2701", action="hold", batch="case", step="hold", timeout_seconds=1)
    api.events["hold"], api.controls["hold"] = [], []
    session = OfflineSession(api, "hold", params)
    result = scenario_algorithm(session, AlgorithmInput(symbol="rb2701", target_volume=1, trade_rule={}, params=params))
    assert result.orders == []
    assert api.volume == 1 and session.native_calls == 0


@pytest.mark.parametrize("checkpoint, expected_native", [("before_send", 0), ("after_send", 1)])
def test_termination_at_send_boundary_never_reissues_and_only_cancels_owned(tmp_path, checkpoint, expected_native):
    from axile.executor.algorithms.core.base import AlgorithmInput

    from ft_ctp.algorithm import ScenarioParams, scenario_algorithm
    from support import OfflineSession

    _, api = make_runner(tmp_path)
    params = ScenarioParams(
        symbol="rb2701",
        action="hold",
        batch="case",
        step=checkpoint,
        timeout_seconds=1,
        **{f"{checkpoint}_hold_seconds": 3},
    )
    api.events[checkpoint], api.controls[checkpoint] = [], []
    session = OfflineSession(api, checkpoint, params)

    def terminate_on_gate(seconds):
        if seconds == 3:
            session.terminated = True
        session.handle_termination_checkpoint()

    session.sleep_or_terminate = terminate_on_gate
    result = scenario_algorithm(session, AlgorithmInput(symbol="rb2701", target_volume=1, trade_rule={}, params=params))
    assert session.native_calls == expected_native
    assert len(result.orders) == expected_native
    assert all(order.status == "已撤销" for order in result.orders)
    assert api.volume == 0


def test_busy_rejection_still_terminates_first_owned_task(tmp_path):
    runner, api = make_runner(tmp_path, pause_point="queued")
    request = api.request
    triggers = []

    def busy(method, path, body=None, **kwargs):
        if path == "/account/execute/4":
            triggers.append(path)
            if len(triggers) == 2:
                raise ApiError("已有执行，不能排队", 409)
        return request(method, path, body, **kwargs)

    api.request = busy
    run_scenarios(runner, ["pause"])
    assert runner.bundle.index["scenarios"][0]["status"] != "passed"
    assert api.executions["offline-0001"]["status"] == "TERMINATED"
    assert not runner.pending_execution_ids
    assert (runner.bundle.path / "restored.json").exists()


def test_pause_auto_timeout_budget_includes_gate_and_order_wait(tmp_path):
    runner, api = make_runner(tmp_path, pause_action="pause_auto")
    runner.preflight()
    runner.prepare()
    runner.configure("hold", "gate", hold_seconds=runner.config.timeout_seconds)
    assert api.account["execution_timeout"] >= 2 * runner.config.timeout_seconds + 30


def test_default_cli_connects_to_axile_and_reads_selected_credentials(tmp_path, monkeypatch):
    config = Config(results_dir=str(tmp_path / "results"))
    api = OfflineApi(config)
    reads = []

    def selected(database, name):
        reads.append((database, name))
        return {"id": 4, "account_config": {"password": "fixture-password"}}

    monkeypatch.setattr("ft_ctp.cli.load_account", selected)
    monkeypatch.setattr("ft_ctp.cli.Api", lambda *a: api)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"results_dir": str(tmp_path / "results"), "symbol": config.symbol}))
    assert main(["run", "--config", str(path), "--batch", "default"]) == 0
    assert reads and api.counts["place_order"] > 0
    assert main(["run", "--config", str(path), "--batch", "default"]) == 2


def test_connect_failure_preserves_actual_connection_error_and_never_triggers_trade(tmp_path, monkeypatch):
    config = Config(results_dir=str(tmp_path / "results"))
    api = OfflineApi(config)
    request = api.request

    def failed_connection(method, path, body=None, **kwargs):
        if path.endswith("/connection"):
            return {"status": "unavailable", "last_failure": {"error": {"reason_code": "4097"}}}
        if path.endswith("/connection/reconnect"):
            raise ApiError("CTP 交易前置已断开", 502)
        return request(method, path, body, **kwargs)

    api.request = failed_connection
    monkeypatch.setattr("ft_ctp.cli.load_account", lambda *a: {"id": 4, "account_config": {"password": "fixture"}})
    monkeypatch.setattr("ft_ctp.cli.Api", lambda *a: api)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"results_dir": str(tmp_path / "results"), "symbol": config.symbol}))
    assert main(["connect", "--config", str(path), "--batch", "connection-failed"]) == 2
    saved = json.loads((tmp_path / "results/connection-failed/connection.json").read_text())
    assert saved["last_failure"]["error"]["reason_code"] == "4097"
    assert not api.executions


def test_core_noop_requires_real_target_and_zero_complete_accounting():
    from ft_ctp.checks import find_core_results, judge_core_noop

    config = Config()
    result = {
        "algorithm": "FT-CTP-SCENARIO",
        "symbol": config.symbol,
        "status": "NOOP",
        "target_volume": 1,
        "orders": [],
        "trades": [],
        "memory": {},
    }
    artifacts = {"data": [{"content": {"symbol_results": {config.symbol: result}}}]}
    assert find_core_results(artifacts) == [result]
    assets = {"source": "real", "positions": [{"symbol": config.symbol, "direction": "多头", "volume": 1}]}
    pending = {"complete": True, "data": []}
    accounting = {"complete": True, "total": 0}
    assert judge_core_noop("open", result, config, assets, pending, accounting)[0] == "passed"
    for incomplete in ({"complete": False, "total": 0}, {"complete": True, "total": 1}):
        assert judge_core_noop("open", result, config, assets, pending, incomplete)[0] == "unknown"
    assert judge_core_noop("close", result, config, assets, pending, accounting)[0] == "unknown"
    assets["positions"][0]["volume"] = 2
    assert judge_core_noop("open", result, config, assets, pending, accounting)[0] == "unknown"


def test_quota_anchor_does_not_spend_timeout_in_artificial_hold(tmp_path):
    runner, api = make_runner(tmp_path, pause_point="quota")
    run_scenarios(runner, ["pause"])
    params = next(iter(api.sessions.values())).params
    assert params.hold_seconds == 0
    assert runner.bundle.index["scenarios"][0]["status"] == "passed"


def test_active_account_is_not_reconfigured(tmp_path):
    runner, api = make_runner(tmp_path)
    original = copy.deepcopy(api.account)
    api.executions["someone-else"] = {"status": "RUNNING"}
    run_scenarios(runner, ["open"])
    assert runner.bundle.index["scenarios"][0]["status"] == "insufficient_conditions"
    assert api.account == original
    assert not api.sessions


def test_new_pending_order_between_scenes_prevents_execution_prep(tmp_path):
    runner, api = make_runner(tmp_path)
    runner.preflight()
    runner.prepare()
    request = api.request

    def pending(method, path, body=None, **kwargs):
        if path.endswith("/orders?refresh=true"):
            return {"complete": True, "data": [{"order_id": "unrelated"}]}
        return request(method, path, body, **kwargs)

    api.request = pending
    with pytest.raises(Exception, match="场景开始前账户出现挂单"):
        runner.trade("open", "01-open")
    assert not api.sessions


def test_unknown_contract_uses_exact_query_without_recycling_session(tmp_path):
    runner, api = make_runner(tmp_path, invalid_case="unknown")
    request = api.request
    disconnected = False
    recoveries = []

    def catalog(method, path, body=None, **kwargs):
        nonlocal disconnected
        if path == "/instrument-catalog/query":
            assert body["symbols"] == ["FT_UNKNOWN_CONTRACT"]
            assert "context.get_quote" not in api.portfolio["custom_calc_py_code"]
            raise ApiError("CATALOG.NOT_FOUND: 合约查询返回空结果或合约已下市", 404)
        if path == "/account/4/connection" and disconnected:
            return {"status": "closed"}
        if path == "/account/4/connection/reconnect":
            recoveries.append(path)
            disconnected = False
        return request(method, path, body, **kwargs)

    api.request = catalog
    run_scenarios(runner, ["invalid"])
    assert runner.bundle.index["scenarios"][0]["status"] == "passed"
    assert not recoveries
    assert not api.sessions
    assert (runner.bundle.path / "final-residue.json").exists()


def test_cleanup_termination_409_race_still_confirms_terminal_and_restores(tmp_path):
    runner, api = make_runner(tmp_path)
    runner.preflight()
    original = copy.deepcopy(runner.original)
    runner.prepare()
    runner.configure("hold", "pause", hold_seconds=2)
    execution_id = runner.trigger("pause")
    request = api.request

    def finished_before_termination(method, path, body=None, **kwargs):
        if path.endswith("/terminate"):
            api.executions[execution_id]["status"] = "SUCCEEDED"
            raise ApiError("账户当前没有活跃执行", 409)
        return request(method, path, body, **kwargs)

    api.request = finished_before_termination
    runner.stop_owned()
    runner.restore()
    assert not runner.pending_execution_ids
    assert runner.active_execution is None
    assert all(api.account.get(k) == v for k, v in original.items())
    assert (runner.bundle.path / "restored.json").exists()


def test_quota_anchor_rejects_unrelated_query_throttling():
    from ft_ctp.scenarios import waiting_for_test_quota

    hit = {"operation": "query_account", "rule_kind": "per_minute", "scope_type": "account", "limit": 0}
    monitor = {"waiting": [{"kind": "quota", "hits": [hit]}]}
    assert not waiting_for_test_quota(monitor)
    hit["operation"] = "place_order"
    assert waiting_for_test_quota(monitor)
    hit["limit"] = 30
    assert not waiting_for_test_quota(monitor)


@pytest.mark.parametrize("checkpoint", ["before_send", "after_send"])
def test_real_termination_propagates_without_five_second_final_query(tmp_path, checkpoint):
    from axile.executor.algorithms.core.base import AlgorithmInput
    from axile.executor.termination import ExecutionTerminated

    from ft_ctp.algorithm import ScenarioParams, scenario_algorithm
    from support import OfflineSession

    _, api = make_runner(tmp_path)
    params = ScenarioParams(
        symbol="rb2701",
        action="hold",
        batch="case",
        step=checkpoint,
        timeout_seconds=1,
        **{f"{checkpoint}_hold_seconds": 3},
    )
    api.events[checkpoint], api.controls[checkpoint] = [], []
    session = OfflineSession(api, checkpoint, params)

    def stop(seconds):
        if seconds == 3:
            raise ExecutionTerminated(reason="stop", mode="cancel_pending", cancel_unconfirmed=True)

    session.sleep_or_terminate = stop
    with pytest.raises(ExecutionTerminated):
        scenario_algorithm(session, AlgorithmInput(symbol="rb2701", target_volume=1, trade_rule={}, params=params))
    assert not session.order_callbacks and not session.trade_callbacks
    assert session.native_calls == (checkpoint == "after_send")
