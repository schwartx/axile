# 跳过未消费的 WBT DataFrame

> 历史归档：候选实现及完整基准脚本见提交 `66feb7a`，基线见下文。`benchmark_performance_frames.py` 的旧 worker、合成生成器和独立 CLI 已删除；模块仅保留存活读取基准共用的采样与夹具检查函数。以下命令须在当时的候选 checkout 运行，不适用于当前 Rust 分析链路；测量数据与原始证据保留。

> 历史测量：以下默认公共构造器契约现已退役，旧实现只保留在 `tests/unit/server/_performance_oracle.py`。复现整链路数字需要当时的基线与候选 checkout；当前生产不提供 Python 计算或 `AnalysisManager`。

基线为 Axile main `9794361`，改动仅涉及 `performance.py`：权益账本消费的是 `participants` 和统计，不读取 `BacktestInput.frame`。在账本计算的两次调用中关闭权重表构造；`build_wbt_input()` 默认行为仍生成原有列、float64 类型和退出零权重行。不改变计算公式、存储、迁移、依赖、API 或前端。

## 验收方法

通过相同 Python 环境、两个独立源码目录、每轮全新进程和独立 SQLite 工作副本对比。基线/候选交替运行各三轮，以下为中位数。包含完整 FastAPI 应用和路由装配、实际 AnalysisManager 加载/计算/发布、三个区间快照、完整绩效与成本汇总的真实 ASGI HTTP 响应。每个进程处理同一负载，使用相同线程配置，操作系统文件缓存为热缓存。后台交易、调度及柜台恢复生命周期在两组均不启动。

进程峰值取操作系统 `ru_maxrss`；阶段峰值由外部每5毫秒采样；稳定值为请求结束后1秒内的RSS中位数。不强制GC或清理分配器。每个API响应完整业务数据规范化后逐项哈希比较；仅排除新快照随机标识与生成时间，保留数值、null、缺失、数组顺序及完整结果字段。另有单元测试逐字比较完整计算模型JSON，并覆盖缺目标、空目标、退出行情缺口、恢复、零手续费、非零完整精度手续费、三个区间和空输入；禁止账本调用创建任何DataFrame。默认公共构造器保留原权重表。

测试环境：macOS，同一 Python 3.12.12、pandas 3.0.5、NumPy 2.3.2、SQLAlchemy 2.0.52、SQLite 3.50.4。核心相关测试128项通过；配套Axon main `bb8fb2f7` 的发行层160项通过、1项跳过；Ruff、C901≤15和改动生产文件BasedPyright通过。未改前端，未部署。

## 数据与结果

真实性样本为未清空 raw_input 的 CHEN 一致副本，26条CTP执行（23条非空输入），SHA256：

`27745d4b7722e5c5769ca56d9f502503503b0eba4edd0cefd6f7e0dbd7222a99`

该副本不是四渠道覆盖证明。规模样本由脚本在独立副本中生成5000条执行，每条20个品种、完整目标与盘口、资产变化；这部分完全为合成数据，成交明细为空，专门验证多品种曲线计算的开销，不冒充真实历史或成交压力场景。两组均通过真实写入器计算新快照，不使用历史重算结果替代迁移比较；本次没有迁移。

| 指标 | CHEN 原版 | CHEN 候选 | 合成原版 | 合成候选 |
|---|---:|---:|---:|---:|
| 计算阶段耗时，秒 | 0.01304 | 0.00665 | 1.2361 | 1.0610 |
| 加载、计算、发布总耗时，秒 | 0.3673 | 0.3599 | 1.9329 | 1.7572 |
| 进程峰值，MiB | 201.73 | 199.84 | 462.88 | 459.67 |
| 请求后稳定RSS，MiB | 201.72 | 199.83 | 461.86 | 459.66 |
| API请求组合耗时，秒 | 0.04319 | 0.04331 | 0.62293 | 0.62892 |

规模样本中计算阶段峰值由404.25降至388.67 MiB，加载/计算/发布阶段峰值由425.69降至412.83 MiB。完整业务API结果在所有运行中一致，原始CHEN文件哈希未变。

本次收益主要是减少无用计算：规模案例计算约快14%，整条计算发布约快9%。整进程峰值只减少约3.2 MiB，API耗时基本持平；不能据此声称已经解决数百MB常驻占用。三轮短测不代表长期运行或所有负载。

## 历史复现

在基线源码目录和候选源码目录均保留时，用同一已安装开发依赖的Python运行：

```bash
python scripts/benchmark_performance_frames.py /ABS/CANDIDATE SOURCE_0033.db /NEW/PRIVATE/real --baseline /ABS/BASELINE --runs 3
python scripts/benchmark_performance_frames.py /ABS/CANDIDATE SOURCE_0033.db /NEW/PRIVATE/scale --baseline /ABS/BASELINE --runs 3 --synthetic-records 5000 --symbols 20
```

源码目录必须为绝对路径，数据库必须是封存、已checkpoint的本地副本。输出目录必须不存在。输出日志和运行数据库只写入指定私有目录，报告不包含原始账户/执行内容。工作数据库可写，原始数据库不会被修改。脚本逐个验证实际导入的源码目录，禁用出站连接。每个子进程限时60秒。示例完整运行比普通单元测试长，应单独安排。

本次私有报告目录：`/tmp/axile-unused-frames-study-real-v3`、`/tmp/axile-unused-frames-study-scale-v1`。业务数据库不入库。
