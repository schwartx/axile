# 通知函数

账户通知函数接收执行事件的脱敏快照。可以用 `def notify(context)` 或 `async def notify(context)` 定义入口，只能接收一个位置参数。

## 最小可运行示例

下面的示例只打印本地摘要，不发送外部消息：

<<< ../examples/notify_summary.py

## 事件与可选字段

| 字段 | 含义 |
| --- | --- |
| `event_type` | `execution.finished` 或 `supplement.cancelled` |
| `account` | 账户 ID、名称、市场和渠道，不包含连接凭据 |
| `execution` | 执行 ID、状态、结论、错误与时间；取消补发事件中可为 `None` |
| `assets` | 资产快照；部分金额可能不可用 |
| `targets` | 当前目标、上次目标与目标数量 |
| `positions` | 持仓与目标信息；资产不可用时可能为 `None` |
| `orders` / `trades` | 本次订单与成交，列表可能为空 |
| `symbols` | 品种状态、目标与订单成交计数 |
| `summary` | 汇总计数，包含 `trade_count` |
| `default_feishu_variables` | 默认飞书卡片模板变量 |
| `is_test` | 当前调用是否为试跑 |

取消补发事件可能只有账户、补发安排和上次执行信息，`summary` 等执行字段也可能缺失。读取 `execution`、资产金额、持仓和执行 ID 前，要检查是否有值。

## 试跑与保存

账户已有执行记录后，点击“试跑函数”，使用最近一次执行的脱敏快照运行当前草稿，不用先保存，也不会为获取试跑数据连接渠道。可选择执行完成或取消补发事件；两类试跑的顶层 `is_test` 都为 `True`。执行完成事件还保留 `execution.is_test`，取消补发事件的 `execution` 为 `None`。

试跑可能发送真实外部消息。接入外部服务的函数可以先检查：

```python
def notify(context):
    if context.get("is_test"):
        return
    # 在这里处理真实事件，并为外部请求设置超时
```

后续账户通知只使用已保存的函数。每个账户最多保存一个，清空后保存即可关闭通知。

## 运行边界

函数在独立进程中运行，最长 15 秒，普通返回值会被忽略。异步入口会等待完成，入口结束后取消尚未完成的后台任务。不支持生成器入口。

通知失败时会记录错误，交易结果不受影响，也不会自动补发默认飞书卡片。外部请求的超时要短于函数的运行时限。

账户飞书 Key 通过子进程环境变量 `AXILE_ACCOUNT_FEISHU_KEY` 提供。密钥不要放进通知正文或日志。默认飞书通知本身也是可编辑函数。

系统执行告警位于“系统告警”，账户执行通知位于账户设置，它们接收的事件与字段不同。完整账户通知定义见[通知上下文源码](https://github.com/zengbin93/axile/blob/main/axile/common/notification_context.py)。
