# 06 · 日常运行与配置

目标：让读者设置运行节奏，理解算法等待、执行总超时、流控和通知分别控制什么。

![定时排程预览](../../site/assets/screenshots/06-daily/01-schedule-preview.png)

图说：快捷排程可以选择日盘收盘、固定间隔和夜盘，并预览实际执行时间与补发安排。页面时间为北京时间。本次演示账户最终保持自动执行关闭。

| 图片 | 位置 | 说明重点 |
| --- | --- | --- |
| [排程预览](../../site/assets/screenshots/06-daily/01-schedule-preview.png) | 新建账户向导的「定时」 | 节奏、交易日历、夜盘、补发预览 |
| [通知函数](../../site/assets/screenshots/06-daily/02-notification-function.png) | `/accounts/1/edit/notification` | 选择试跑事件、运行当前草稿、保存函数 |
| [流控概览](../../site/assets/screenshots/06-daily/03-execution-control.png) | `/accounts/1/edit/control` | 预设、账户合计、按品种、操作分类 |
| [下单规则](../../site/assets/screenshots/06-daily/04-rate-rules.png) | 流控页展开「下单」 | 每分钟额度、每日额度、最小间隔、超额排队或阻断 |
| [品种控制](../../site/assets/screenshots/06-daily/05-symbol-controls.png) | `/accounts/1/edit/symbols` | 禁投与风险品种的含义、批量输入 |
| [系统告警](../../site/assets/screenshots/06-daily/06-system-alert.png) | `/settings` | 系统执行异常告警与账户执行通知的设置入口不同 |

算法配置复用 [向导交易方式](../../site/assets/screenshots/03-first-portfolio/06-trading-method.png)；可选算法目录见 [公共算法](../../site/assets/screenshots/08-reference/04-public-algorithms.png)。正文应分别解释算法的最大等待时间和账户执行总超时。等待结束不保证所有目标到位，结果需要到执行详情中确认。

通知使用 [notify_summary.py](examples/notify_summary.py)，只打印本地摘要；本次试跑没有外部消息发送。真实通知函数可能在试跑时发送消息，应讲清 `is_test`、草稿与保存状态。

品种控制截图使用 au2612 作为禁投示例、cu2611 作为风险品种示例，主组合仅包含 rb2701 和 ag2612。图中的流控额度是当前预设展示，不是针对所有账户和渠道的参数建议。
