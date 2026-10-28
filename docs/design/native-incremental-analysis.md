# 后端增量分析与五表模型

基线：Axile next bfdd7b01。替代 PR #125 的 WASM 提议。

删除原有 19 张 performance_* 派生表及旧快照查询、暂存、复制和发布代码。不迁移旧缓存，不保留 snapshot_id 接口；业务事实保持原样。

新增五张类型化表：analysis_state 保存账户/区间的版本、游标、基线和当前日标量状态；analysis_replay_position 保存两种模拟口径的当前持仓；analysis_contribution_state 保存当前合约贡献账本；analysis_day 保存日级收益成果；analysis_contribution_day 保存日级合约贡献成果。原 19 表全部删除，新增 5 表，净减少 14 表；account_analysis 另行精简为源版本和任务协调状态。

执行、成交、持仓、成本明细、事件、绑定及日历从规范事实按需读取，不新增事实镜像。成本按账户和选定范围计算，不持久化展示副本。

普通追加只读取新增执行并推进当前账本，更新当前日和新增日期。30/90 日基线移动仅重放相应区间。历史修订、删除、迟到和同时间记录替换可先安全重放。事实修改与脏标记同事务；计算使用一致只读快照，提交前核对源版本及领取身份，状态、日结果和贡献在一个短写事务中提交。失败、重启和丢失回执不提前推进游标。

前端使用单调修订号，状态和数据按版本对齐。按需事实读取要求相同源版本；发生变化返回版本冲突并重新读取，不提供旧内部接口兼容层。

## 保存边界

当前状态必须持久化，才能在重启后接着上一条执行计算。当前持仓保存模拟账本的数量、单位和标记价格；贡献余额保存期初金额、净现金流、累计费用及质量计数。贡献状态仅额外保留当前日最后一次估值前的四个标量，用于日内新观察替换暂定日末，不保存成交 ID 集合或历史观察副本。

日收益和日贡献是历史数值成果，提供曲线、区间比较和品种排名。基线移动会重放对应 30/90 日区间；all 区间继续续算。没有历史检查点，因此历史修订、删除和迟到记录先重放受影响区间，最坏需要重放账户全历史。业务事实修订在同一事务里推进 source_version 并标记最早受影响记录。

原有渠道日历事实 `analysis_calendar_day` 不属于这 19 张结果表；普通追加只补当前日到新增日期。执行、事件和绑定读取受查询的 SQLite 一致视图及源版本约束。成本请求仍可能扫描所选区间成交；这属于按需查询，后台增量任务不预计算展示明细。

## 原 19 表逐项替代

| 删除表 | 用途与替代 |
| --- | --- |
| performance_snapshot | 版本、状态、基线改由 analysis_state 与精简 account_analysis 保存；没有快照代际 |
| performance_result_range | 区间标量合入 analysis_state |
| performance_result_point | 日级收益成果存 analysis_day，基准锚点从 state 生成 |
| performance_result_skip | 当前累计计数和首末时间在 state |
| performance_result_fallback | 当前累计质量状态在 state，日标记在 analysis_day |
| performance_result_binding | 从 portfolioaccount 按需读取 |
| performance_result_calendar_range | 从渠道日历事实按需合并日期 |
| performance_result_event | 从执行和 schedule_skip、绑定事实按需读取 |
| performance_result_daily_cost | 从范围内成交按需汇总 |
| performance_result_daily_fee | 同一次按需成本汇总按币种返回 |
| performance_cost_execution | 执行头、摘要按需读取和计算 |
| performance_execution_fee | 从该执行成交按币种汇总 |
| performance_cost_trade | 直接读取 business_trade 及 sizing/tick 依据 |
| performance_cost_execution_symbol | 直接读取 execution_symbol_result |
| performance_cost_position | 直接读取业务资产绑定及持仓 |
| performance_cost_attempt | 从 sizing 和订单事实按需计算 |
| performance_cost_transaction | 从所选执行的订单、成交、sizing 按需分组 |
| performance_cost_transaction_fee | 在上述分组中按币种汇总 |
| performance_contribution | 当前余额在 contribution_state，日级数值成果在 contribution_day |

升级删除可重建的旧缓存，不复制旧结果。降级只恢复旧缓存结构，业务事实不变。前后端、原生扩展与迁移 head=0039 同一发行，启动和 wheel 构建均校验原生能力。
