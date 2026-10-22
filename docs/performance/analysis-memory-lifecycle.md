# 绩效全链路内存：分批提炼历史与解除快照缓存引用

> 历史归档：候选实现及完整基准脚本见提交 `6a54b99`，基线见下文。`benchmark_analysis_memory.py` 及旧合成生成器已删除。以下命令须在当时的候选 checkout 运行，不适用于当前 Rust 分析链路；测量数据与原始证据保留。

基线为 Axile main `2254770`。本次围绕后台历史加载、绩效计算、快照发布及实际绩效 HTTP 浏览测量，生产改动仅在 `performance_analysis.py`。数据库结构、存储格式、逻辑版本和计算引擎版本保持不变。

## 查明的对象与读取路径

1. `AnalysisManager._load` 原先把全部执行的输入、结果及 ORM 实例同时装入列表。5,000 条、每条 20 品种的样本中，应用导入约 173 MiB，加载历史后约 304 MiB。独立 tracemalloc 诊断中，JSON 解码器创建约 121 万个存活分配、约 77.7 MiB；原始两列文本合计约 11.6 MiB。展开后的对象及 ORM 管理开销远大于磁盘文本。
2. `compute_batch` 在原始执行仍驻留时，再构造观测、执行摘要、成交和三段绩效结果，扩大同时存活的数据集合。
3. `_publish` 使用 `snapshots.insert().values(ranges=ranges, ...)`。直接检查 SQLAlchemy 引擎的编译缓存，确认绑定参数仍引用整份 `ranges`。独立诊断中，计算返回后约 41.6 MiB 的 Pydantic 序列化结果仍存活；它不是业务所需的另一份快照缓存。缓存中的两个参数名可以指向同一个对象，不能算成两份占用。

tracemalloc 只用于定位对象，显著增加自身开销；下述性能对照全部关闭分配跟踪，也不在运行时扫描编译缓存。

## 改动

- 在原有单一 SQLite 读事务中，以字段行分批读取执行，每批 100 条，不再实例化历史执行的 ORM 对象。
- 使用原来的 `observation`、`project_execution` 和事件转换，把本批提炼成计算必需数据后释放原始 JSON 根对象。预处理在工作线程执行，不把整段转换放到事件循环上。
- 保留记录顺序、原始时间字符串、成交顺序及跨批次的稳定事件排序；目标权重回退仍来自同一数据库快照。计算公式、浮点累加顺序和缺失数据处理未改变。
- 只为最终保留执行列表的 `all` 区间组装该列表，省去原先在 `30`、`90` 区间构造后丢弃的列表。
- 快照写入改为 `session.execute(insert, parameters)`，让业务值作为执行参数传入，解除编译缓存对快照的引用。事务、快照版本及上一版本保留逻辑不变。

完整观测、必要成交和最终冻结结果仍需保留，内存没有变成常数；分析数据仍按当前表格式保存 JSON。该阶段解决读取过程及编译缓存中的不必要驻留，没有进行存储迁移。

## 验收与数据来源

同一 macOS、Python 3.12.12、SQLite 3.50.4、SQLAlchemy 2.0.52。基线/候选交替各三轮，每轮全新进程、独立可写副本、相同线程设置、热文件缓存，不强制 GC。完整导入应用，运行实际 AnalysisManager 读取、计算和发布，再通过真实 ASGI HTTP 读取三个区间快照、完整绩效及成本汇总。交易/订阅/调度生命周期未启动，禁止出站连接；本报告是本地绩效链路，不覆盖联机账户 worker 或完整线上服务。

口径补充：此历史 ASGI 基准的验证客户端和应用同进程，RSS 包含客户端解析响应的开销。后续新增了[独立客户端的真实 HTTP 测量](snapshot-execution-values.md)，原版与候选均按新方法重测；两种方法的绝对内存数值不直接串接比较。

系统峰值为 `ru_maxrss`，阶段值为进程 RSS，请求结束后的稳定值为随后 1 秒采样中位数。全链路耗时从 worker 开始到全部 API 完成；另报告从应用导入开始至 API 完成的耗时。操作系统级 Python 启动之前的时间不计入这两个区间。

三类来源严格分开：

- **真实 CHEN 封存副本**：26 条完整执行、23 条非空输入、5 条成交，未清空输入。SHA256：`27745d4b7722e5c5769ca56d9f502503503b0eba4edd0cefd6f7e0dbd7222a99`。
- **CHEN 完整载荷重复扩展**：保留原始输入/结果文本，改变新增记录标识和时间，共 2,000 条执行、385 条成交。它是合成重复负载，不是真实 2,000 次交易。SHA256：`747a5688626c02b815af3c07b6c853658b4b410e166e6328a6994c1926a599a3`。
- **独立合成压力样本**：5,000 条执行、每条 20 个品种，具有完整目标、价格和资产观测，没有成交。用于测历史展开和绩效回放，不作为成交兼容性的唯一证据。SHA256：`889f394beeb0907b41567864e0498828fe5f760f82b00225c34f017db666af44`。

接口比较覆盖全部 JSON 业务值及数组顺序，规范化对象键顺序，仅排除每次新生成的顶层 `snapshot_id` 和 `computed_at`。另外逐行哈希当前快照的账户、设置、原始 ranges 文本、版本、data_until，以及所有执行和成交的非 snapshot_id 列与原始 payload 文本。因此不仅验证响应模型，也验证实际落库内容，保留数值词法和精度。所有运行均一致，源文件哈希未变。

## 三轮中位数

| 指标 | 真实原版 | 真实候选 | CHEN 扩展原版 | CHEN 扩展候选 | 20 品种压力原版 | 20 品种压力候选 |
|---|---:|---:|---:|---:|---:|---:|
| 整进程峰值，MiB | 199.73 | 199.56 | 316.59 | 249.11 | 456.58 | 334.44 |
| 请求后稳定 RSS，MiB | 199.72 | 199.55 | 316.58 | 249.09 | 455.56 | 334.14 |
| 加载、计算、发布，秒 | 0.3621 | 0.3636 | 0.6161 | 0.6115 | 1.7640 | 1.7241 |
| worker 至 API 完成，秒 | 0.4051 | 0.4075 | 0.7798 | 0.7747 | 2.3605 | 2.2405 |
| 应用导入至 API 完成，秒 | 1.2730 | 1.2742 | 1.6500 | 1.6383 | 3.2651 | 3.1139 |

压力样本峰值减少约 **122 MiB（27%）**，全链路耗时减少约 **5%**；CHEN 扩展样本峰值减少约 **67 MiB（21%）**，耗时基本持平；真实小样本基本持平。这次主要收益是内存，不能把后续计算阶段单独缩短的比例当作净提速，因为转换已移入加载阶段。

20 品种压力样本的阶段 RSS：

| 阶段 | 原版 MiB | 候选 MiB |
|---|---:|---:|
| 应用导入后 | 173.45 | 173.19 |
| 历史加载/预处理结束 | 303.64 | 251.81 |
| 后续计算结束 | 378.45 | 299.00 |
| 发布函数结束 | 410.45 | 331.23 |
| worker 返回 | 409.45 | 287.23 |
| 浏览及空闲后 | 455.56 | 334.14 |

## 回归与限制

相关核心 138 项通过；匹配 Axon main `7bbf3a25` 的发行层 160 项通过、1 项跳过。Ruff、C901≤15 通过。首次性能验收时，修改文件的 BasedPyright 与原版具有完全相同的 5 个已有诊断，没有新增。后续收尾明确了快照的可空分支、执行结果字典类型和状态标签的可空键类型；该文件 BasedPyright 已清零，计算和数据处理行为保持不变。

新增回归跨越 100/100/5 三批，对照整段历史的完整计算结果，包含跨批次同时间失败事件、绑定和休市、目标回退、真实零、空目标及状态信息。弱引用检查证实加载完成后原始输入/结果根对象释放；连续发布两份快照后，其内存对象均释放，而数据库仍保留两版。没有使用强制 GC 的测试数字作为性能收益。

仍有约 173 MiB 的应用导入基线、完整结果组装和快照读取开销；必要结果的大小仍随历史和品种增长。未部署，也未验证完整线上服务的驻留占用。

## 历史复现

```bash
git worktree add --detach /ABS/BASELINE 2254770
PYTHONPATH=/ABS/CANDIDATE python -m scripts.benchmark_analysis_memory /ABS/CANDIDATE /PRIVATE/CHEN_0033.db /NEW/PRIVATE/REAL --baseline /ABS/BASELINE --data-kind real
PYTHONPATH=/ABS/CANDIDATE python -m scripts.benchmark_analysis_memory /ABS/CANDIDATE /PRIVATE/CHEN_REPEAT_2000.db /NEW/PRIVATE/REPEAT --baseline /ABS/BASELINE --data-kind chen-repeated
PYTHONPATH=/ABS/CANDIDATE python -m scripts.benchmark_analysis_memory /ABS/CANDIDATE /PRIVATE/CHEN_0033.db /NEW/PRIVATE/SYNTHETIC --baseline /ABS/BASELINE --synthetic-records 5000 --symbols 20
```

使用同一个装有开发依赖的 Python；输入须为已 checkpoint 的封存副本。输出目录须不存在。合成生成器复用 `benchmark_performance_frames.synthetic_source`，只操作私有副本。已有合成文件作为输入时须显式用 `--data-kind synthetic` 标注，`--synthetic-records 0` 只代表没有现场生成，不能据此认定输入真实。

每个子进程最长 60 秒。诊断时可单独设置 `AXILE_ANALYSIS_TRACEMALLOC=1` 或 `AXILE_INSPECT_COMPILED_CACHE=1`，开启后不得将其 RSS/耗时用于常规性能对照。输出包含工作库与私有日志，不应入库。

三轮结果：`/tmp/axile-analysis-final-real-v2`、`/tmp/axile-analysis-final-chen-scale-v2`、`/tmp/axile-analysis-final-scale-v2`。独立对象诊断：`/tmp/axile-analysis-object-profile-v1`、`/tmp/axile-analysis-cache-profile-v1`。
