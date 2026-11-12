"""持仓确认结果使用真实原生存储跨执行恢复，保留账户隔离。"""

from tests.conftest import native_store_call
from tests.unit.server._business_facts_test_support import read_result, seed_account
from tests.unit.server.test_execution_derived_views import persist


def test_position_evidence_roundtrip_and_restart_guard(native_database):
    seed_account(native_database)
    seed_account(native_database, 2)
    row = {
        "symbol": "A",
        "status": "SUCCEEDED",
        "start_volume": 0,
        "expected_volume": 100,
        "position_confirmed": False,
        "final_volume": None,
    }

    def save(identity, symbols, account_id=1):
        return persist(
            native_database,
            account_id=account_id,
            execution_id=identity,
            input_id=identity + "-input",
            result_input_id=identity + "-result-input",
            raw_result={"status": "SUCCEEDED", "success": True, "symbol_results": symbols},
        )

    def pending(account_id=1):
        return native_store_call(
            native_database, "execution_read", {"op": "pending_positions", "account_id": account_id}
        )

    saved = save("pending", {"A": row})
    restored = read_result(native_database, {"id": saved["record_id"]})
    assert restored["symbol_results"]["A"]["start_volume"] == 0
    assert restored["symbol_results"]["A"]["expected_volume"] == 100
    assert restored["symbol_results"]["A"]["position_confirmed"] is False
    assert pending() == {"A": 100}
    assert pending(2) == {}
    save("other-account", {"B": {**row, "symbol": "B", "expected_volume": 200}}, 2)
    save("unrelated-failure", {})
    assert pending() == {"A": 100}
    save("still-pending", {"A": {**row, "status": "BLOCKED"}})
    assert pending() == {"A": 100}
    save("confirmed", {"A": {**row, "position_confirmed": True, "final_volume": 100}})
    assert pending() == {}
    assert pending(2) == {"B": 200}
