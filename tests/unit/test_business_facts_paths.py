"""业务发生来源保持原生标识，并拒绝损坏的路径转义。"""

import pytest

from axile.server.business_facts.paths import join_pointer, split_pointer


@pytest.mark.parametrize("symbol", ["", "0", "A/B~C", "沪深300", "same"])
def test_native_symbol_path_roundtrip(symbol):
    assert split_pointer(join_pointer("/symbol_results", symbol)) == ("symbol_results", symbol)


@pytest.mark.parametrize("path", ["no-root", "/bad~", "/bad~2"])
def test_invalid_origin_escape_is_rejected(path):
    with pytest.raises(ValueError):
        split_pointer(path)
