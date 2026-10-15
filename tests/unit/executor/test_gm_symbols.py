import pytest

from axile.common.gm_symbols import GMSymbolResolver
from axile.common.trade_channel import TradeChannel
from axile.executor.models.unified_input import GMAccountConfig, UnifiedStandardInput


def test_rejects_conflicting_alias_values() -> None:
    resolver = GMSymbolResolver()
    standard_input = UnifiedStandardInput(
        channel_type=TradeChannel.GM,
        account_config=GMAccountConfig(
            connection_mode="terminal",
            account_id="account",
            token="token",
            terminal_path="C:/gm",
        ),
        curr_target={"600000.SH": 0.1, "SHSE.600000": 0.2},
    )

    with pytest.raises(ValueError, match="配置不一致"):
        resolver.normalize_input(standard_input)
