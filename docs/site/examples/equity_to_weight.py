"""通过账户权益将固定名义敞口转换为原始目标权重。"""

from axile.server.context import Context


def calculate_portfolio(context: Context) -> dict[str, float]:
    """读取权益并计算权重；账户杠杆仍在后续执行阶段应用。"""
    equity = context.account.total_asset
    if equity <= 0:
        raise ValueError("账户权益必须大于零")
    return {"rb2701": 100_000.0 / equity}
