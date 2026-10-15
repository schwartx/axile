# 错误展示与连接恢复

`lib/errorPresentation.ts` 统一决定错误由谁展示，`ErrorNotice` 负责呈现。
请求状态保留完整原始错误；展示收口不能删除证据，也不能把读取失败当成空数据。

新增入口必须声明 `intent`：

- `read`：读取失败。全局失联入口存在且已确认网络故障时，退为中性占位或旧数据时间。
- `mutation`：用户主动执行保存、启动、试跑、重算等操作。即使失联也保留直接反馈。
- `uncertain`：已受理操作的终态待确认。失联时保留业务状态，网络原因与重连交给顶部。

传入 `hasData` 表示已有可展示的成功结果；`updatedAt` 表示其成功读取时间。
`variant` 只决定视觉密度，不能代替操作语义。组件额外展示错误颜色、文字或重试按钮时，
也必须使用 `useErrorPresentation` 的判断，避免提示框收起后仍有重复告警。

`GlobalErrorContext` 只挂在实际提供 `ServiceConnectionStatus` 的布局内。
无该入口的初始化页、独立页面和测试夹具保留局部错误，不能隐藏唯一的解释和重试入口。
账户渠道异常用携带账户 ID 的 `AccountConnectionContext` 收口，局部读取入口也传入对应
`accountId`；不能根据错误文本、HTTP 503 或其他账户的异常推断归属。

连接状态沿用共享 dashboard 读取：网络错误表示失联，HTTP 错误表示该接口刷新失败。
只有失联之后 dashboard 成功刷新才广播一次读取恢复，其他接口成功不能解除全局状态。

`usePolling` 已接入读取恢复，包括 `intervalMs: 0` 的一次性读取。自定义状态机通过
`useReadRecovery` 注册读取回调；身份切换、关闭和卸载仍须中止旧请求或使旧响应失效。
回调只重读快照、配置、证据和已知任务 ID，不能注册保存、下单、撤单、任务创建或重算。
恢复时保留编辑草稿；冻结的错误证据只补齐失败来源，不替换已经读取的内容。

验证覆盖 `errorPresentation.test.ts`、`readRecovery.test.ts`、`ErrorNotice.test.ts`
及已有 `e2e/errors.e2e.ts` 的全局断连、恢复、业务操作和草稿场景。
