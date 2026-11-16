from __future__ import annotations

import pytest

from axile.common.trade_channel import TradeChannel
from axile.executor.models.unified_input import TQAccountConfig, UnifiedStandardInput
from axile.executor.tq.symbols import TQInstrument, TQSymbolResolver


def _resolver() -> TQSymbolResolver:
    return TQSymbolResolver(
        [
            TQInstrument("SHFE.rb2610", "rb2610", "SHFE", "FUTURE"),
            TQInstrument("CZCE.CF701", "CF701", "CZCE", "FUTURE"),
            TQInstrument("CZCE.TA701C5000", "TA701C5000", "CZCE", "OPTION"),
            TQInstrument("CFFEX.IF2701", "IF2701", "CFFEX", "FUTURE"),
            TQInstrument("DCE.m2609-C-2700", "m2609-C-2700", "DCE", "OPTION"),
            TQInstrument("KQ.m@SHFE.rb", "m@SHFE.rb", "KQ", "CONT"),
            TQInstrument("SSE.600000", "600000", "SSE", "STOCK"),
        ],
        reference_year=2026,
    )


def test_resolves_derivative_symbols_in_both_directions() -> None:
    resolver = _resolver()

    assert resolver.to_tq("rb2610", for_trade=True) == "SHFE.rb2610"
    assert resolver.to_axile("SHFE.rb2610") == "rb2610"
    assert resolver.to_tq("m2609-C-2700", for_trade=True) == "DCE.m2609-C-2700"


@pytest.mark.parametrize("symbol", ["CF701", "CZCE.CF701", "CF2701", "CZCE.CF2701"])
def test_resolves_czce_four_digit_year_alias(symbol: str) -> None:
    resolver = _resolver()

    assert resolver.to_tq(symbol, for_trade=True) == "CZCE.CF701"
    assert resolver.to_axile(resolver.to_tq(symbol)) == "CF701"


@pytest.mark.parametrize("symbol", ["TA701C5000", "TA2701C5000", "CZCE.TA2701C5000"])
def test_resolves_czce_option_four_digit_year_alias(symbol: str) -> None:
    resolver = _resolver()

    assert resolver.to_tq(symbol, for_trade=True) == "CZCE.TA701C5000"
    assert resolver.to_axile(resolver.to_tq(symbol)) == "TA701C5000"


def test_does_not_compact_non_czce_four_digit_contract() -> None:
    resolver = _resolver()

    assert resolver.to_tq("IF2701", for_trade=True) == "CFFEX.IF2701"
    with pytest.raises(ValueError, match="不属于当前 TqSdk"):
        resolver.to_tq("CFFEX.IF701", for_trade=True)
    with pytest.raises(ValueError, match="不存在"):
        resolver.to_tq("CF3701", for_trade=True)


def test_quote_only_symbol_is_preserved_but_rejected_for_trade() -> None:
    resolver = _resolver()

    assert resolver.to_tq("KQ.m@SHFE.rb") == "KQ.m@SHFE.rb"
    assert resolver.to_tq("SSE.600000") == "SSE.600000"
    assert resolver.to_axile("SSE.600000") == "SSE.600000"
    with pytest.raises(ValueError, match="仅支持行情查询"):
        resolver.to_tq("SSE.600000", for_trade=True)


def test_expired_derivative_is_queryable_but_not_tradable() -> None:
    resolver = TQSymbolResolver([TQInstrument("SHFE.rb2501", "rb2501", "SHFE", "FUTURE", expired=True)])

    assert resolver.to_tq("rb2501") == "SHFE.rb2501"
    with pytest.raises(ValueError, match="仅支持行情查询"):
        resolver.to_tq("rb2501", for_trade=True)


def test_unknown_and_ambiguous_symbols_fail_explicitly() -> None:
    resolver = TQSymbolResolver(
        [
            TQInstrument("SHFE.x2601", "x2601", "SHFE", "FUTURE"),
            TQInstrument("DCE.x2601", "x2601", "DCE", "FUTURE"),
        ]
    )

    with pytest.raises(ValueError, match="不存在"):
        resolver.to_tq("missing")
    with pytest.raises(ValueError, match="多个天勤代码"):
        resolver.to_tq("x2601")


def test_normalizes_all_standard_input_symbol_fields_without_mutating_source() -> None:
    resolver = _resolver()
    standard_input = UnifiedStandardInput(
        channel_type=TradeChannel("tq"),
        account_config=TQAccountConfig(
            account_mode="kq",
            tq_username="user",
            tq_password="password",
        ),
        curr_target={"CF2701": 0.2},
        last_target={"CZCE.CF701": 0.1},
        symbol_algorithms={"CF2701": {"method": "SINGLE-MAKER", "params": {}}},
        trade_rules={"CZCE.CF2701": {"min_notional": 100}},
        forbidden_symbols=["CF2701", "CF701"],
        risk_symbols=["CZCE.CF2701"],
    )

    normalized = resolver.normalize_input(standard_input)

    assert normalized.curr_target == {"CF701": 0.2}
    assert normalized.last_target == {"CF701": 0.1}
    assert list(normalized.symbol_algorithms) == ["CF701"]
    assert normalized.trade_rules == {"CF701": {"min_notional": 100}}
    assert normalized.forbidden_symbols == ["CF701"]
    assert normalized.risk_symbols == ["CF701"]
    assert standard_input.curr_target == {"CF2701": 0.2}


def test_rejects_conflicting_czce_alias_values() -> None:
    resolver = _resolver()
    standard_input = UnifiedStandardInput(
        channel_type=TradeChannel("tq"),
        account_config=TQAccountConfig(
            account_mode="kq",
            tq_username="user",
            tq_password="password",
        ),
        curr_target={"CF2701": 0.1, "CF701": 0.2},
    )

    with pytest.raises(ValueError, match="配置不一致"):
        resolver.normalize_input(standard_input)
