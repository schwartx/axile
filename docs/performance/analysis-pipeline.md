# 分析发布与读取链路

Python 负责业务事实、HTTP 和任务协调；Rust 的 `publish_store` 负责原生分析的计算、结果暂存和原子发布。前端从 Python 读取已发布版本，用执行摘要绘图，选中执行后再加载详情。

## 领取、计算与发布

1. Python 通过 CAS 领取账户当前源版本，写入 `running_version` 和独立的 `running_token`，并准备观测、成本源事实与日历。0037 后，Python 维护类型化业务事实及其分析源投影，执行输入、结果、资产和目标权重不再整份追加到旧 JSON 列；存储与迁移边界见[执行业务事实的结构化存储](business-facts.md)。
2. Python 给 Rust 发送账户、预期源版本、token、设置和渠道等控制字段；`snapshot_id` 与本次 token 相同。业务库路径由服务端解析。
3. Rust 在一致的只读源事务内计算三个绩效区间，通过进程内回调校验类型化结果行，并写入磁盘暂存 SQLite。结果行不经 Python，也不通过 stdout 返回。
4. 暂存完整后结束源读事务，再开启主库 `BEGIN IMMEDIATE` 发布事务。这里再次检查源版本、领取版本/token、账户设置与渠道；不会把旧读事务直接升级成写事务。
5. 同一事务写入快照、绩效结果、成本副本、执行摘要和费用，设置读取完成标记，更新当前/上一快照与任务状态，并清理更旧的版本。
6. Rust 只返回发布状态、身份、版本、数据时间及可选行数等小回执。Python 核对回执，并以已提交数据库状态为准恢复任务。

源事实在计算期间变化，或另一个任务已取得领取身份时，旧任务不能覆盖新结果；释放与失败处理也必须同时匹配版本和 token。同一快照身份的重试核对已有发布结果。提交后的 stdout 丢失、进程退出或取消并不表示事务失败。

Rust 不写原始执行、资产、目标权重或渠道配置，不执行数据库迁移。计算、磁盘暂存和主库最终写事务分开；SQLite 写锁竞争仍可能使最终事务等待或失败，需要按实际日志模式与并发负载验证。

## 冻结结果与迁移

已有 `performance_result_*` 与 `performance_cost_*` 继续保存曲线、日成本、事件及执行明细。0036 增加：

| 存储 | 用途 |
| --- | --- |
| `performance_execution_summary` | 按 `(snapshot_id, record_id)` 冻结时间、金额、成本、覆盖率、计数及是否存在成交事务；索引 `(snapshot_id, time, record_id)` 支持稳定读取 |
| `performance_execution_fee` | 按快照、执行与币种冻结费用及覆盖笔数，保留零费用和返佣 |
| `performance_snapshot.read_view_schema_version` | 1 表示新读取视图完整，包括零执行快照；0 表示需要兼容处理或刷新 |
| `account_analysis.running_token` | 区分同一源版本下的不同任务领取 |

摘要不按 day/range 重复保存；区间从冻结的时间边界筛选。执行头字段仍保存在 `performance_cost_execution`，读取时按快照与执行关联。历史成本只累计同时具备金额与成本证据的成交；缺失金额、未知覆盖率、缺失品种数、未知持仓与空持仓继续区分。

0036 只为结果版本 2、成本版本 1 的完整类型化快照补齐视图，不解析旧 JSON。无法补齐的历史保持标记 0。Python 显式兼容发布在能够结构化时也准备同样的摘要；不能结构化时保留原兼容结果。降级删除 0036 的视图和新增字段，保留 0035 结果、成本事实及其兼容读取能力。

## 读取与页面换版

新接口位于 `/api/v1/account/performance/{account_id}`，沿用现有认证与账户存在校验：

| 接口 | 数据与版本要求 |
| --- | --- |
| `/status` | 小型状态与当前已发布版本，不返回曲线或执行详情 |
| `/chart?snapshot_id=S&range=R` | 固定版本的曲线、日成本、事件、执行索引及费用；执行索引只有标量摘要 |
| `/execution-summary?snapshot_id=S&range=R` | 分页读取已冻结的执行摘要，可带视窗时间范围；请求时不聚合成交 |
| `/execution-detail?snapshot_id=S&record_id=ID` | 该次执行的头信息、摘要、持仓、尝试、品种状态、成交和费用；明细分页返回后续位置 |

表格采用 `columns/rows`，响应携带 `schema_version` 与 `snapshot_id`。每次数据读取保持同一数据库视图，后续明细继续使用页面已展示的快照身份。新接口不会启动 Rust 查询子进程，也不会读取 `ranges/payload` 并恢复整段历史。

页面首次获取状态后加载指定版本。之后同一版本只轮询状态；发现新版本时先读取其图表数据，成功后整体替换展示版本。曲线、日成本、摘要、详情和成本链接不会提前切到另一个版本。图表 hover 使用摘要，选中执行后加载详情；时间轴使用曲线与真实执行时间，不为导航补造成交时间。

已淘汰的快照返回 410；跨账户的快照或该版本不存在的执行返回 404；标记 0 返回 409 `ANALYSIS_READ_VIEW_UNAVAILABLE`。页面据此检查状态或提供刷新入口，不在读取请求中回退为完整历史 JSON。状态错误与结果读取错误分别保留，失败的新结果不会替换仍可展示的旧版本。

当前 chart 可以返回所选区间的全部轻量执行索引。它省掉历史持仓、成交和状态子树，大小仍随曲线点和执行数量增长；这不构成固定响应大小或性能收益承诺。

## 兼容命令与运行要求

`publish` 的 JSONL 帧协议与原有 `performance/replay/costs/snapshot/performance_result` 查询保留兼容入口。它们的完整结果传输和 Python 发布行为不代表新 `publish_store` 的职责。

部署新协调代码前，核对二进制 `capabilities` 的 `protocol=1`，以及 `commands` 和 `write_commands` 都包含 `publish_store`。旧版本字符串、可运行或旧查询成功不能替代此项核对。构建使用冻结源码和工作树之外的 target；服务运行时不执行 Cargo。构建、暂存与资源设置见[SQLite 结构化事实与 Rust 分析](rust-sqlite-analysis.md#开发构建)。

## 已发布结果读取测量

2026-10-20 使用同一份 Python 源码、同一个 release Rust 二进制，对照旧完整 `/snapshot` 与新 `/status`、`/chart`、单次 `/execution-detail`。输入是已通过实际 `publish_store` 发布的冻结合成 SQLite：1000 次执行、每次 10 个品种，共 10000 笔成交；所选详情有 10 条成交事务，持仓和尝试均为空。三轮各读取五次，计时与内存采样使用不同服务进程；每次响应的共同字段语义均通过对照，新读取启动 Rust 子进程次数为 0。

| 指标 | 旧完整快照 | 新读取 |
| --- | ---: | ---: |
| 首次 HTTP 耗时中位数 | 116.94 ms | 状态＋图表合计 65.03 ms |
| 首屏响应字节 | 4,848,300 | 状态＋图表合计 202,133 |
| Python＋Rust 同时 RSS 采样峰值 | 248.39 MiB | 161.42 MiB |

测量使用真实 loopback HTTP、独立客户端和只挂性能路由的隔离服务（router-only），关闭应用 lifespan；客户端解析与绘制不计入上述 HTTP 耗时。RSS 每 2 ms 从外部合计 Python 服务与 Rust 后代，排除客户端和测量驱动；共享页可能重复计数，短峰值可能漏采。它比较同一代码中的两种读取入口，不是历史版本回归对照，也不代表完整服务驻留内存或真实账户性能。完整发布耗时、写入锁竞争、生产负载与生产验收均未在这组读取测量中验证。

在 Axile 仓库根目录复现；先将 `AXILE_ANALYSIS_BENCH_BINARY` 指向已构建并冻结的 release `axile-analysis`。两个命令使用同一二进制，脚本会保存二进制副本、服务源码清单、输入哈希与只含汇总数据的 `report.json`：

```bash
: "${AXILE_ANALYSIS_BENCH_BINARY:?请设置已构建的 release axile-analysis 路径}"
analysis_bench_dir="$(mktemp -d)"
uv run --no-sync python scripts/benchmark_analysis_views.py prepare \
  "$analysis_bench_dir/synthetic.sqlite" \
  --binary "$AXILE_ANALYSIS_BENCH_BINARY" --observations 1000 --symbols 10
uv run --no-sync python scripts/benchmark_analysis_views.py \
  "$analysis_bench_dir/synthetic.sqlite" "$analysis_bench_dir/read-comparison" \
  --binary "$AXILE_ANALYSIS_BENCH_BINARY" --account-id 1 --range all \
  --data-kind synthetic --runs 3 --iterations 5
```

本次二进制 SHA256 为 `7c52f89b4299b13692ca7ed39250decd5f259eb92245a5657487cf438127e15a`，服务源码清单 SHA256 为 `989a3f4b49cb7746de6499219af5797f731340ca69e677709a8932be99fd7395`。本次 review 的完整报告另存于 `/private/tmp/axile-analysis-redesign-20261020/perf-fixture/read-comparison-3/report.json`；目录清理后仍可按上述命令重新生成报告，新构建应保留各自的实际哈希。
