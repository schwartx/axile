"""自定义函数组合路由契约测试。"""

import asyncio

import pytest

from axile.server.portfolio_function import _normalize_target

CODE = "def calculate_portfolio(context):\n    return {'rb2610': 1.0}\n"


@pytest.mark.parametrize(
    "target",
    [[], {1: 0.5}, {"rb2610": True}, {"rb2610": "0.5"}, {"rb2610": float("inf")}],
)
def test_target_must_be_finite_symbol_weight_mapping(target: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _normalize_target(target)


@pytest.mark.parametrize("account_id", [None, 7])
@pytest.mark.parametrize("valid", [True, False])
def test_run_and_save_persists_only_successful_trial(monkeypatch, account_id, valid):
    from unittest.mock import AsyncMock, Mock

    from axile.server.api.routes import portfolio as routes
    from axile.server.db.models import Portfolio, ValidateCustomCalcResponse
    from axile.server.db.models.portfolio import RunAndSavePortfolioRequest

    original_code = CODE + "# original\n"
    portfolio = Portfolio(name="before", market="futures", custom_calc_py_code=original_code)
    account = Mock()
    session = Mock()
    session.get = AsyncMock(side_effect=[portfolio] + ([account] if account_id else []))
    session.rollback = AsyncMock()
    run = AsyncMock(return_value=ValidateCustomCalcResponse(valid=valid, target={"rb2610": 1.0} if valid else None))
    append = AsyncMock()
    clear = Mock()
    monkeypatch.setattr(routes, "_run_custom_calc_validation", run)
    monkeypatch.setattr(routes, "append_target_weight_snapshot", append)
    monkeypatch.setattr(routes, "try_register_target_refresh", Mock(return_value=True))
    monkeypatch.setattr(routes, "clear_target_refresh", clear)

    result = asyncio.run(
        routes.run_and_save_portfolio(
            session, 3, RunAndSavePortfolioRequest(name="after", custom_calc_py_code=CODE, account_id=account_id)
        )
    )

    assert result.valid is valid
    run.assert_awaited_once_with(account if account_id else None, CODE)
    clear.assert_called_once_with(3, account_id)
    if valid:
        assert portfolio.name == "after"
        assert portfolio.custom_calc_py_code == CODE
        append.assert_awaited_once_with(
            session,
            portfolio_id=3,
            account_id=account_id,
            raw_weights={"rb2610": 1.0},
            normalized_weights=None,
            source="manual",
        )
    else:
        assert portfolio.name == "before"
        assert portfolio.custom_calc_py_code == original_code
        append.assert_not_awaited()
