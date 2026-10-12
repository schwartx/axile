# 08 · 进阶参考

完成使用指南后，可以在这里查询函数契约、字段、渠道差异与算法入口。

| 你要做什么 | 查看哪里 |
| --- | --- |
| 读取账户、持仓、行情、订单或成交 | [Context 查询接口](/reference/context) |
| 接收执行事件，配置账户通知 | [通知函数](/reference/notifications) |
| 选择下单算法，理解参数和扩展入口 | [执行算法](/reference/algorithms) |
| 安装渠道依赖，配置环境、日志和算法目录 | [渠道与配置](/reference/channels) |
| 理解截图来源、演示凭据和验证范围 | [截图与演示数据](/reference/screenshots) |

## 内置开发说明

组合编辑器中包含 Context 和函数契约说明。本站将常用内容整理成可以直接访问的参考页。

![内置组合函数契约和上下文说明](../assets/screenshots/08-reference/01-function-contract.png)

## 两种自定义开发

**组合函数**回答“想持有什么”：读取账户或信号，返回原始目标权重。入口是 `calculate_portfolio(context)`。

**执行算法**回答“如何完成目标”：消费已规划的单品种目标数量，管理下单、等待、撤单和结果。它需要理解渠道语义、终止处理和执行审计，开发前先看[算法参考](/reference/algorithms)。
