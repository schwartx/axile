# SQLite 结构化事实与 Rust 分析

> 本文保留分析改造时的运行边界、命令和历史测量。当前第一方生产 SQLite 的查询、业务写入、初始化与迁移由进程内 Rust 绑定负责，Python 使用普通 Pydantic 模型；现行约定见 [Rust SQLite 存储边界](../architecture/rust-sqlite-storage.md)。下文有关 Python ORM、迁移和直接读取数据库的描述属于当时实现，历史测量不代表当前存储实现的性能。

这条实现保留 SQLite。Python 保存类型化业务事实、准备结构化源数据并提供 HTTP；Rust 接收小型分析任务，自行读取事实、计算、暂存结果，再通过事务写入分析表并发布快照。0037 后的业务存储和升级方式见[执行业务事实的结构化存储](business-facts.md)。

绩效页通过 Python 直接读取已发布的曲线、日成本和执行摘要；选中执行后才读取持仓、成交等详情。新页面读取不启动 Rust 子进程，也不重建完整历史执行对象。任务、发布与前端换版的细节见[分析发布与读取链路](analysis-pipeline.md)。

## 当前存储范围

0034—0036 的分析改造保留了原始业务 JSON，并增加结构化投影。0037 进一步迁移执行输入、结果、资产与目标权重，删除对应旧 JSON 列，业务读写统一使用类型化事实。前后端一起发行，不保留每条执行分别选择旧 JSON 或新结构的运行时分支。

| 数据 | 后续写入方式 |
| --- | --- |
| 执行输入、结果 | 稳定字段写类型化列和子表；执行前冻结输入，终态引用该输入；详情显式组装 |
| 资产、目标权重快照 | 资产与持仓写独立观测，快照保存引用；目标权重写有序明细，不再追加对应完整 JSON |
| 执行事件、审计附件、账户控制等证据 | 独立上下文继续按职责保存；输入、目标、规则和前后资产附件以 schema 3 引用不可变事实 |
| 账户连接、执行、调度、通知和回测配置 | 配置变更时继续更新 JSON，部分字段仍使用 Pydantic 模型 |
| 执行、成交、持仓等分析源事实 | 写入类型化列和子表，Rust 正常分析读取这些投影 |
| 可结构化的新分析结果和成本快照 | 实际内容写类型化表，旧 `ranges/settings/payload` 列仅写 `{}` 占位 |
| 历史分析快照兼容或数据库降级 | 既有分析快照的格式兼容继续保留；业务库降级须先检查新证据能否由旧结构完整表示 |

当前稳定业务字段以类型化事实为准，分析投影与冻结结果继续分别承担计算和版本读取职责。动态配置、渠道扩展和异常历史字段仍按所属对象保留 JSON；这不等于删除整个系统中的 JSON，也不保证所有数据分布的磁盘占用都会下降。

新原生发布命令 `publish_store` 的进程间通信只有小请求和小回执。结果行在 Rust 进程内进入磁盘暂存 SQLite，不经过 Python 的 JSON 编解码。旧查询仍可能编码完整响应；旧 `publish` JSONL 帧协议已移除，发布使用 `publish_store`。Python 使用标准库 `json`，Rust 使用 `serde_json`，没有进行全局 `orjson` 替换，也没有移除整个后端的 Pydantic 或 SQLAlchemy。

Python 的 `wbt` 包和没有业务引用的 Polars 依赖已移除，锁文件同时裁剪它们独占引入的 Plotly、Polars runtime。生产 Python 的 `build_wbt_input()`、`calculate_performance()` 与 `performance_replay.replay_daily()` 已退役，原公共 helper 导入和 DataFrame 返回契约不再支持；不提供兼容 wrapper。旧权益账本仅保存在 `tests/unit/server/_performance_oracle.py`，供业务断言和源码 benchmark 对照，不随生产包提供。`Observation`、`observation()`、`local_time()`、`close_before()` 仍用于生产事实准备及账户摘要；`ENGINE_VERSION` 归属 `performance_analysis.py`，值保持不变。pandas、NumPy、Arrow 仍被渠道 SDK、数据接口或 Streamlit 使用，没有随 WBT 一并删除。该变更清理安装依赖，不代表 Python 服务驻留内存会按包安装体积等量下降。

## 运行边界

原生部分是按任务启动的独立可执行程序 `axile-analysis`，不监听额外 HTTP 端口。Python 负责认证、参数校验、领取任务、源事实准备、子进程生命周期和失败恢复；成功的原生发布事务由 Rust 完成。

| 工作 | Python 职责 | Rust 职责 |
| --- | --- | --- |
| 交易、策略调用、调度、账户与组合管理 | 执行业务逻辑；账户列表仍由 Python ORM 和响应模型处理 | 当前不接管 |
| 源事实准备 | 保存类型化业务事实、维护分析投影、分批补齐缺失投影和日历元数据 | 校验并读取已提交的事实 |
| 权益、收益及目标权重回放 | 协调任务，不执行 Python 回放 | 执行账户收益计算、目标回放和回测收益计算 |
| 新绩效页读取 | 在固定版本内读取状态、曲线、日成本、执行摘要和单次执行详情 | 请求时不参与 |
| 原有成本、绩效和快照接口 | 保留鉴权与兼容入口，适用时转发原生查询结果 | 保留筛选、聚合、排序、分页及旧完整结果读取 |
| 原生后台刷新 | 领取带 token 的任务、准备源事实、核对回执和持久化结果 | 一致读取、计算、磁盘暂存、校验、CAS 发布和快照保留清理 |
| 数据库迁移和业务写入 | 负责迁移、类型化业务事实、分析源事实及任务领取 | 仅原生发布命令写分析结果与对应发布状态，不写业务源事实 |

```text
交易与执行结果 → Python 写类型化业务事实、维护分析源投影
                                      ↓
Python 领取任务 → 小请求 → Rust 读取、计算、暂存、事务发布 → 小回执
                                      ↓
前端 → Python HTTP → 已发布 SQLite 曲线、日成本、执行摘要
                        └─ 选中执行后读取该次详情
```

两边不共享 Python/Rust 对象引用。`publish_store` 在 Rust 内部复用计算输出回调，校验并暂存标量行；这些行不会经 stdin/stdout 交给 Python。前端表格采用 `columns/rows` 形式传输，客户端将这些轻量表格转换成展示数据，不恢复完整历史执行对象。

数据库文件路径由 Python 服务的实际数据库会话解析，不能由前端查询参数指定。Rust 从现有文件打开数据库，不执行业务库迁移，也不因路径错误创建新业务库。计算使用独立只读事务；源读事务结束后，发布使用独立写事务。当前生产分析只支持文件型 SQLite，不再提供内存数据库或其他数据库后端的 Python 计算回退。

新旧命令采用不同的数据交接方式：

- `publish_store`：小请求包含账户、源版本、claim token、设置等控制信息；Rust 自行计算和发布，只返回有界回执，不返回曲线或执行行。
- 新 `status/chart/execution-summary/execution-detail` HTTP：Python 直接读已发布表，不调用原生查询程序。
- 原有原生 `performance/replay/costs/snapshot/performance_result` 查询：保留完整 JSON 响应入口，适用时由 Python 转发原生编码字节。

原生子进程结束后，其读取缓存和计算内存随进程释放。计算、源事实复制、磁盘暂存及最终提交仍有成本；曲线和区间执行摘要仍随历史增长，详情分页也需要实际读取。这些边界不等于固定内存或已完成生产负载验收。

## 数据与迁移

分析数据分为源事实和已发布结果。新数据的分析读取不再依赖完整历史 JSON：

| 数据 | 结构化存储 | 写入和读取职责 |
| --- | --- | --- |
| 回放观测 | `replay_observation`、`replay_observation_symbol` | Python 在业务事实写入后维护投影；Rust 按时间读取 |
| 执行与成本源事实 | `analysis_source_execution`、`analysis_source_trade` 及品种、持仓、尝试、成交、手续费子表 | Python 在执行写入边界投影一次；Rust 分析时直接读，不再逐次解码原执行 JSON |
| 日历元数据 | `analysis_calendar_day` | Python 按渠道、日期准备小型元数据；Rust 按日期读取 |
| 已发布成本 | `performance_cost_*` 标量与明细表 | 原生发布由 Rust 在发布事务中复制源事实 |
| 已发布绩效结果 | `performance_result_range`、`performance_result_point`、`performance_result_skip`、`performance_result_fallback`、`performance_result_binding`、`performance_result_calendar_range`、`performance_result_event`、`performance_result_daily_cost`、`performance_result_daily_fee` | 原生计算和发布由 Rust 完成；新页面由 Python 直接读列 |
| 执行摘要与费用 | `performance_execution_summary`、`performance_execution_fee` | 发布时按快照与执行冻结一次，区间按时间筛选，不按 range/day 重复保存整份摘要 |

`performance_snapshot.result_schema_version = 2` 表示绩效结果存于上述标量表，设置使用 `backtest_weight_type`、`backtest_fee_rate` 标量列；新快照的旧 `settings`、`ranges` 列仅保留空对象兼容表结构。`cost_schema_version = 1` 表示成本明细类型完整，新快照不再向成本 `payload` 写完整 JSON。字段为空与未知、缺失身份与显式 null 等语义继续由类型列和存在标记区分，不能简单补零或强转字符串。

迁移 `0034_analysis_rust_facts` 建立类型化成本与回放基础；`0035_analysis_structured_results` 建立源事实、日历和规范化结果。源事实表初始为空，由写入边界或历史准备阶段补齐。可完整转换的旧快照升级为结果版本 2，但仍保留原 JSON 以支持降级；无法完整转换的旧快照继续标记为 0。Rust 读取版本 2 时不会选择这些残留 JSON 列。

迁移 `0036_analysis_read_views` 增加执行摘要、分币种费用、`account_analysis.running_token` 和 `performance_snapshot.read_view_schema_version`。只有结果版本 2、成本版本 1 的完整类型化快照会从冻结事实补齐读取视图，并标为 `read_view_schema_version=1`，包括零执行快照。无法补齐的旧快照保持 0；新接口明确返回视图不可用，不在读取请求中展开历史 JSON。降级移除新增视图与字段，保留 0035 的结果和成本事实。

0037 在同一保存点内转换和核对业务证据，通过后删除对应旧 JSON 列。缺失分析投影时，从规范业务事实按有界批次补齐；输入、目标和间接资产绑定等子事实变化时，相应投影失效并推进源版本。缺失、过期或不支持的源投影会使分析任务进入失败及重试处理，不恢复整段旧业务 JSON，也不能生成成功零结果。该行为与下面既有已发布分析快照的格式兼容分别处理。

日历事实记录 `available_open`、`available_closed`、`unavailable` 或 `not_required`；未注册渠道使用内部 `unregistered` 标记，公开结果仍为 unavailable 且不生成区间。Python 每次准备分析时重新评估观测覆盖日期，按 128 行批次写入；它反映当前外部日历元数据，不是不可变历史。Rust 缺少某日期时按 unavailable 处理。账户渠道变化会推进源数据版本，触发新渠道的日历准备与分析。

旧快照的版本标记为 `0` 时保留兼容能力：历史成本通过明确的 Python 旧查询读取，即使设置 `AXILE_ANALYSIS_MODE=required` 也保留这项旧数据例外；历史绩效快照可由 Rust 的旧格式读取器处理。只有这条兼容路径会读取旧快照 JSON。已选择 Rust 后的实际执行错误不会触发静默重试 Python。

原生发布时，Rust 在最终事务中以 `INSERT … SELECT` 将类型化源事实复制到快照表，校验身份、投影版本及成交数量。执行汇总在发布阶段冻结；新执行摘要读取不重新聚合成交。手续费保留币种、真实零费用和返佣，未知金额继续为空。

Rust 完成计算和磁盘暂存后先结束源读事务，再以 `BEGIN IMMEDIATE` 取得发布事务，核对源版本、`running_version/running_token`、当前账户设置及渠道。结果、成本、摘要、完成标记、当前/上一快照指针和成功状态一起提交；只保留当前与上一版本。

源版本或领取身份变化时，过期任务不能发布，也不能释放别人的领取状态。数据库中的已提交快照决定成功与否；提交后回执丢失、进程退出或请求取消，Python 按本次 token 对应的快照核对结果，不把已提交任务重新标成失败。

部署时继续使用项目现有的迁移流程，不要从独立 Rust 程序执行迁移。首次迁移、旧数据回填、正式启用和性能比较应先在数据库副本上完成；本文中的命令不会自动修改生产数据库。

## 开发构建

需要 Rust 工具链与 C 编译环境；SQLite 通过 `rusqlite` 的 bundled 功能编译，不依赖生产主机上的 Python SQLite 扩展。Cargo.lock 随源码提供，构建使用 `--locked`。

在冻结的 Axile 源码目录构建，target 放到工作树之外；同一个 target 由一个构建进程使用。下面是 macOS/Linux 示例，Windows 的可执行文件带 `.exe`：

```bash
export CARGO_TARGET_DIR="${TMPDIR:-/tmp}/axile-analysis-target"
cargo build --locked --release --manifest-path rust/analysis/Cargo.toml --target-dir "$CARGO_TARGET_DIR"
"$CARGO_TARGET_DIR/release/axile-analysis" --version
"$CARGO_TARGET_DIR/release/axile-analysis" capabilities
```

能力声明必须包含 `protocol=1`、`commands` 中的 `publish_store`，以及 `write_commands` 中的 `publish_store`。旧二进制的版本字符串或旧查询成功不足以证明支持新发布协议。服务运行时不会自动执行 Cargo。

开发服务可通过 `AXILE_ANALYSIS_BINARY` 指向核对过的外置程序；打包前再将本平台程序暂存到 `axile/server/_native/` 并保留可执行权限。target 和暂存程序均不入库。

若本机配置的 `RUSTC_WRAPPER` 因缓存守护进程失效而阻塞，可仅对本次构建清除该设置后重试：

```bash
RUSTC_WRAPPER= cargo build --locked --release --manifest-path rust/analysis/Cargo.toml --target-dir "$CARGO_TARGET_DIR"
```

## 启用边界（旧回退已退役）

生产刷新必须使用原生分析程序；`AXILE_ANALYSIS_MODE=python` 不再是可用回退方案，`auto` 也不提供 Python 计算。`AXILE_ANALYSIS_BINARY` 可指定预先构建的程序。缺少原生能力应明确失败，不得返回旧 Python 账本结果。

| 资源设置 | 默认值 | 作用 |
| --- | --- | --- |
| `AXILE_ANALYSIS_TIMEOUT_SECONDS` | `120` | 包含排队和执行的任务期限 |
| `AXILE_ANALYSIS_MAX_CONCURRENCY` | `2` | 每个 Python 事件循环允许同时运行的原生任务数 |
| `AXILE_ANALYSIS_MAX_OUTPUT_BYTES` | `67108864` | 兼容查询响应的总输出字节上限；新发布回执另限至多 64 KiB |

请求限制为 1 MiB，错误输出限制为 64 KiB。超时或请求取消时终止并回收子进程。多个 Python worker 的总并发需要按 worker 数一起估算，不能把上述并发设置当作整台主机的全局限制。

## Wheel 打包

先按上节完成外置 target 构建和能力核对，再暂存本平台程序。例如，在 Axile 根目录运行：

```bash
uv run python - <<'PY'
import os
import shutil
from pathlib import Path

filename = "axile-analysis.exe" if os.name == "nt" else "axile-analysis"
source = Path(os.environ["CARGO_TARGET_DIR"]) / "release" / filename
destination = Path("axile/server/_native") / filename
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(source, destination)
PY
uv build --wheel
```

构建钩子只在暂存了当前平台的程序时打包它，并生成 `py3-none-<本机平台>` wheel；本地 Linux 构建不会自动声称 manylinux 或 musllinux 兼容。不能将缺少原生程序的包作为支持 Python 计算回退的发行物。

这不是跨平台构建方案：需要分别在目标系统和架构构建、验证，不应把其他机器生成的程序复制过来再用本机标签打包。源码分发不携带已编译程序。正式发行前还需要按现有发布门禁检查 wheel 内容、目标系统动态库兼容性和隔离安装。该功能没有自动发布或安装到生产环境。

## 验收重点

先比较同一份数据库副本、相同查询条件下的响应语义；重点覆盖快照过期、分页游标、空值、手续费币种、跨日成交及回放缺行情。再比较整套 Python 与原生子进程的合计驻留、峰值、任务结束后的回落，以及后台分析期间账户列表的尾延迟。同时测量并发写入时的 WAL 增长、检查点推进和发布事务耗时；单次只读事务及源事实复制的工作量仍随历史增长。

只比较 Rust 进程的空闲占用、单个响应大小或单次查询耗时，不能证明整套服务已经降低内存。数据库升级、缺失源事实补齐和历史分析快照兼容路径也必须纳入测量。

已有证据分别记录在[独立回放测量](native-replay-measurement.md)与[旧发布链测量](structured-publication-measurement.md)。它们测量的是独立回放或旧的 Python 发布链，不能替代新版 `publish_store`、轻量读取和按需详情的整链路对照。新版仍需同一冻结数据集上的响应语义、合计 RSS、首次读取、状态轮询、详情查询和并发发布验证；本文不提供新版最终性能数或生产验收结论。
