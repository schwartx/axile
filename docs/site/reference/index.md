# 进阶参考

函数参数、模型字段、渠道差异和算法接口可按下表查询。

| 你要做什么 | 查看哪里 |
| --- | --- |
| 读取账户、持仓、行情、订单或成交 | [Context 查询接口](/reference/context) |
| 接收执行事件，配置账户通知 | [通知函数](/reference/notifications) |
| 选择下单算法，理解参数和扩展入口 | [执行算法](/reference/algorithms) |
| 安装渠道依赖，配置环境、日志和算法目录 | [渠道与配置](/reference/channels) |

## 内置开发说明

组合编辑器内也有 Context 和函数契约说明。常用内容在本站的参考页中可以直接查阅。

![内置组合函数契约和上下文说明](../assets/screenshots/08-reference/01-function-contract.png)

## 两种自定义开发

组合函数读取账户或信号，返回各品种的原始目标权重，入口是 `calculate_portfolio(context)`。

执行算法接收已规划的单品种目标数量，处理下单、等待、撤单和执行结果。编写时需要了解渠道语义、终止处理和执行审计，接口见[算法参考](/reference/algorithms)。
