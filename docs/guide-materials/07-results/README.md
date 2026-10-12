# 07 · 查看结果与排错

目标：能从执行结果找到具体品种、订单、成交和剩余缺口。

![部分成交的执行详情](../../site/assets/screenshots/07-results/04-partial-execution.png)

图说：rb2701 的可执行目标是多 12 手，本次仅从 6 手变成 7 手，仍差 5 手；另一品种完成。详情明确显示“执行未全部完成”。到位比例 7/12 与本轮委托成交率 1/6 是两个口径。

| 图片 | 场景 | 说明重点 |
| --- | --- | --- |
| [完整执行](../../site/assets/screenshots/07-results/01-completed-execution.png) | 完整成交夹具 | 账户结论、各品种结果、执行链路、原始附件入口 |
| [订单与成交](../../site/assets/screenshots/07-results/02-order-and-fill.png) | 展开完整执行的订单树 | 委托、成交、方向、数量和订单终态 |
| [无需调仓](../../site/assets/screenshots/07-results/03-noop.png) | 持仓已达到可执行目标 | 没有新增成交可以是正常结果 |
| [部分执行](../../site/assets/screenshots/07-results/04-partial-execution.png) | 一个品种留有缺口 | 实际持仓、可执行目标、差额、原因 |
| [部分成交后撤单](../../site/assets/screenshots/07-results/05-partial-order.png) | rb2701 委托 6 手、成交 1 手、剩余撤销 | 成交记录和撤单终态同时存在 |
| [偏离总览](../../site/assets/screenshots/07-results/06-deviation-dashboard.png) | 部分成交后回到控制台 | 发现需要关注的账户 |
| [执行记录列表](../../site/assets/screenshots/07-results/07-execution-list.png) | 完成、空跑、部分完成三种记录 | 按次执行查看结果与成交规模 |
| [按品种筛选](../../site/assets/screenshots/07-results/08-symbol-filter.png) | 切换「按品种」，搜索 rb2701 | 缩小到单个品种排查 |
| [绩效页面](../../site/assets/screenshots/07-results/09-performance.png) | 同一天的三次夹具执行 | 账户收益、回测口径、成交点、执行异常时间线 |

建议排查顺序：先看账户结论，再看未完成品种；核对目标数量、执行前后实际持仓和剩余缺口；展开订单终态与成交；需要追溯时再看执行链路及原始附件。

本次三种执行结论来自专用离线结果夹具，并经过真实 Axile 持久化、审计和页面展示。`DOCS.PARTIAL_FILL` 是夹具原因码，不能写成公共算法的正式错误码。本次没有验证交易所拒单、真实超时、断线恢复或真实流控阻断。

绩效图只有同一天的夹具观测，价格与权益保持固定，收益和滑点均为零。用于说明界面和口径，不能作为策略表现或成交质量证据。正式文档若需要多年曲线，须另外准备明确标注来源的演示历史。

另有账户概览成交摘要与详情不一致的问题，见 [采集发现](../support/findings.md)。正文以执行详情中留存的订单与成交证据为准。
