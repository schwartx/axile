# Rust SQLite 分析

Rust 通过进程内 PyO3 扩展 `axile.server._native._core` 执行 SQLite 业务读写、迁移和分析发布。Python 保留 HTTP、策略、渠道 SDK、Clock 和调度。调用、事务、连接复用与取消契约见 [Rust SQLite 存储边界](../architecture/rust-sqlite-storage.md)。

## 计算与发布

Python 的 `AnalysisManager` 领取持久化任务并准备渠道日历，向 `_core.publish_store` 传入完整请求。Rust 在一致的只读快照中读取规范业务事实，计算 `30`、`90`、`all` 三个区间的绩效、成本与执行摘要。权益账本的引擎版本为 `equity-ledger-1`；缺失目标和缺失行情的观测按既有规则跳过，空目标仍表示清仓。

计算代码使用固定参数化 SQL，逐行直接写入私有磁盘暂存 SQLite；不再传递 JSON 行帧。暂存库限制页面缓存为 4 MiB 并关闭 mmap，不再额外构造通用行批次；计算所需的持仓和日级结果沿用现有内存边界。计算结束后，Rust 在 `BEGIN IMMEDIATE` 事务中重新核对源版本、领取 token、账户设置和渠道，一起提交结果、成本、摘要及快照指针。旧任务不能发布到更新的源版本，也不能释放其他任务的领取状态。回执丢失或取消时，Python 按 token 核对已提交快照。

已发布结果使用类型化结果表：`result_schema_version=2`、`cost_schema_version=1`、`read_view_schema_version=1`。旧结果格式的兼容读取与历史迁移继续保留。事实结构和证据语义见[执行业务事实的结构化存储](business-facts.md)。

时间解析和浮点毫秒换算由 Rust 共用实现；历史无时区时间按上海本地时间解释。日志、品种活动和观测扫描先规范化时间，再筛选、排序和分页，原始时间与错误证据不回写。分析逻辑版本 `20` 会重新计算旧快照，修正微秒区间边界、历史时间格式和旧结果的展示差异。

绩效状态、曲线、成本和执行详情均通过原生扩展读取；需要 HTTP 透传的查询直接返回 JSON 字节，内部业务查询返回 Python 对象。旧 CLI、命令分发和专用 wire 返回路径已移除。服务不启动分析子进程，也不在运行时调用 Cargo。

## 构建与打包

需要 Rust 工具链和 C 编译环境，SQLite 由 `rusqlite` bundled 功能编译。冻结源码后在工作树之外选择任务专用 target，同一 target 同时只运行一个构建进程：

```bash
export CARGO_TARGET_DIR="${TMPDIR:-/tmp}/axile-native-target"
python scripts/build_native_analysis.py --target-dir "$CARGO_TARGET_DIR"
```

脚本以 `--locked --lib --features python` 构建 release 扩展，隔离加载验证后暂存到 `axile/server/_native/_core.abi3.so`（Windows 为 `_core.pyd`）。已有构建产物可使用 `--no-build`，开发构建可使用 `--debug`；服务不会回退到 Python 存储实现。

```bash
uv build --wheel
```

构建钩子要求扩展通过加载、入口和版本检查：`PUBLIC_HEAD=0039`、`ANALYSIS_FACT_SCHEMA=40`。两者分别是迁移修订号与分析事实版本。wheel 标签为 `cp312-abi3-<本机平台>`；本地构建不自动声明 manylinux 或 musllinux 兼容，需要在目标系统和架构分别构建、验证。

tag 发布流程先构建、验证原生扩展并运行 Rust 单测，再构建 wheel；Linux wheel 经 auditwheel 审核、修复后才上传 PyPI，不上传包含本机扩展的源码发行包。

## 验证与测量

Rust 单测可独立运行，不需要 Python feature：

```bash
cargo test --locked --manifest-path rust/storage/Cargo.toml --lib
```

Python 的真实事务、连接复用和取消契约分别见 `tests/integration/server/test_native_storage_transactions.py`、`tests/integration/server/test_native_connection_reuse.py` 和 `tests/unit/test_native_store_bridge.py`。发行验收还需检查实际 wheel 的扩展、静态资源及隔离安装。

当前原生发布和查询测量使用 `scripts/benchmark_structured_publication.py`、`scripts/benchmark_analysis_views.py`，具体参数见各脚本的 `--help`。历史[独立回放测量](native-replay-measurement.md)和[旧发布链测量](structured-publication-measurement.md)记录了此前 CLI 架构，不能作为当前进程内实现的性能结果。

记录身份排序和去重由 SQLite 的磁盘临时存储完成，基线选择只保留三个区间的候选。Rust 不再保存每条记录的时间索引和权益映射，内存主要随当前持仓、日期和必要返回结果增长。性能比较应覆盖完整 Python 进程的 RSS、并发发布时的接口延迟、WAL 增长及提交耗时，不能只用单次查询或扩展文件大小推断生产收益。
