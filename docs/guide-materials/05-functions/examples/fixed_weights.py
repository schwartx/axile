"""返回固定原始目标权重的教学示例。"""

from axile.server.context import Context


def calculate_portfolio(context: Context) -> dict[str, float]:
    """返回目标权重，正数为多头，负数为空头。"""
    return {"rb2701": 0.4, "ag2612": -0.25}
