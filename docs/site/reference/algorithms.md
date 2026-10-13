# 执行算法

组合函数返回原始目标权重，执行层将它换算为可执行数量。下单算法负责处理目标持仓与当前持仓之间的差量，主交易和清仓可以分别配置算法。

## 当前公共算法目录

![天勤账户可选的公共下单算法](../assets/screenshots/08-reference/04-public-algorithms.png)

| 算法 | 用途 | 需要理解的边界 |
| --- | --- | --- |
| TARGET-POS-TASK | 期货目标持仓，适用于 CTP 与天勤 | 开平仓顺序、报价、等待与追单 |
| SINGLE-MAKER | 单边挂单，可按条件撤单追价 | 主动报价仍可能留下剩量 |
| TWAP | 按时间安排拆单 | 前片欠量滚入后片，计划时长不是严格截止时间 |
| POV | 按收到的增量成交量与参与率跟单 | 无量等待；到期补单可能不再受参与率约束 |
| CTP_OPTION_EXERCISE | CTP 期权行权、放弃或自对冲指令 | 独立指令用途，不用于账户主交易或清仓槽位 |

下拉框只列出适用于当前渠道、主交易或清仓槽位的算法。其他算法仍可能存在于代码库中。

## 参数要结合算法语义看

以 TWAP 为例：

```python
algorithm = {
    "method": "TWAP",
    "params": {
        "total_duration": 300,
        "slices": 10,
        "price_strategy": "ACTIVE",
        "max_wait_seconds": 60,
    },
}
```

这组参数计划每 30 秒执行一片，每片最多等待 `min(60, 30)` 秒。未完成的数量会滚入后续片，最后一片可能集中处理剩余数量。执行结束后仍需检查持仓和订单状态，确认是否到位。

这组配置只作参数示例，不作为实际账户的执行建议。

## 继续阅读现有实现

- [期货目标持仓](https://github.com/zengbin93/axile/blob/main/axile/executor/algorithms/defaults/ctp_target_pos_task/README.md)
- [单边挂单](https://github.com/zengbin93/axile/blob/main/axile/executor/algorithms/defaults/single_maker/README.md)
- [TWAP](https://github.com/zengbin93/axile/blob/main/axile/executor/algorithms/defaults/twap/README.md)
- [POV](https://github.com/zengbin93/axile/blob/main/axile/executor/algorithms/defaults/pov/README.md)

## 开发自定义下单算法

自定义算法通过 `register_algorithm` 注册，接收 `AlgorithmInput`，返回 `AlgorithmResult`。输入中包含单品种 `symbol`、已经规划的 `target_volume`、品种交易规则和参数；算法通过 `ExecutorProtocol` 与执行上下文交互。

配置“用户算法目录”并重启，系统就会扫描其中的 Python 模块，加载已注册算法。界面根据元数据中的参数模型、渠道和槽位显示参数并筛选可选算法。

编写算法时需要处理以下问题：

1. 当前渠道的订单参数、开平仓和数量规则。
2. 未知持仓、缺少行情、未完成订单和终态处理。
3. 协作式终止、超时、撤单与剩余缺口。
4. 明确的 `AlgorithmResult`、订单与成交证据、审计事件。
5. 使用 Clock 与执行会话的等待方法，保持可测试性。

接口定义见[算法注册与协议](https://github.com/zengbin93/axile/blob/main/axile/executor/algorithms/core/base.py)，加载过程见[加载器](https://github.com/zengbin93/axile/blob/main/axile/executor/algorithms/core/loader.py)。这里列出的是接口和现有实现，尚无完整的自定义算法开发案例。
