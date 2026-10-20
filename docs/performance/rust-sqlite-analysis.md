# SQLite 结构化事实与 Rust 分析

这条实现保留 SQLite 作为事实存储，由 Python 事务写入，Rust 只读历史事实并执行收益回放、成本聚合和查询。没有新增 DuckDB、Polars 查询引擎或 Parquet 存储，也没有替换整个后端的 Pydantic。

Python 继续处理认证、参数校验、任务协调、数据库迁移和快照发布。Rust 接收账户、快照和查询条件等小请求，从同一个 SQLite 文件直接读取结构化字段；历史数据不先转换成 Python ORM 对象或大字典再传入 Rust。

## 当前改造范围：分析投影，原始 JSON 仍继续写入

当前实现增加了结构化分析投影，尚未将原始业务 JSON 全部替换为 SQLite 列。新执行记录继续保存 `raw_input/raw_result`，并在同一事务维护执行和回放结构化事实；因此同一份业务证据仍同时存在原始 JSON 和供分析使用的类型化投影。

| 数据 | 后续写入方式 |
| --- | --- |
| 原始执行输入、结果 | 每次执行继续写入 `executerecord.raw_input/raw_result` JSON |
| 原始资产、目标权重快照 | 继续追加 `assets`、`raw_weights/normalized_weights` JSON，所需回放数据由结构化投影维护 |
| 执行事件、审计附件、账户控制等证据 | 对应 `details/content/metadata` 等 JSON 继续按业务流程写入 |
| 账户连接、执行、调度、通知和回测配置 | 配置变更时继续更新 JSON，部分字段仍使用 Pydantic 模型 |
| 执行、成交、持仓等分析源事实 | 写入类型化列和子表，Rust 正常分析读取这些投影 |
| 可结构化的新分析结果和成本快照 | 实际内容写类型化表，旧 `ranges/settings/payload` 列仅写 `{}` 占位 |
| 历史兼容数据或数据库降级 | 旧 JSON 保留；兼容发布或降级恢复仍可能写入完整分析 JSON |

所以已经移除的是正常分析路径对原始大 JSON 的反复展开，以及新分析快照的完整大 JSON 复制。原始 JSON 的追加增长仍然存在，结构化列尚未成为唯一存储来源；不能将本次实现描述为“全站消除 JSON”或“停止写入原始 JSON”。

Python 桥接层的小请求和发布帧仍使用标准库 `json`，Rust 使用 `serde_json`，没有进行全局 `orjson` 替换。Rust 的最终响应组装仍有 `serde_json::Value` 对象树和增长数组，完整快照响应仍可能较大。当前实现没有实现全程零拷贝，也没有移除整个后端的 Pydantic 或 SQLAlchemy。

## 运行边界

原生部分是按任务启动的独立可执行程序 `axile-analysis`，通过 stdin/stdout 交换控制请求和 JSON 结果，不监听额外 HTTP 端口。已有 Python HTTP 入口保留认证和接口路径；适用的查询直接返回 Rust 编码的响应字节，避免再次构造完整 Pydantic 响应对象。后台发布协调仍由 Python 负责。

| 工作 | Python 职责 | Rust 职责 |
| --- | --- | --- |
| 交易、策略调用、调度、账户与组合管理 | 执行业务逻辑；账户列表仍由 Python ORM 和响应模型处理 | 当前不接管 |
| 源事实准备 | 保存原始业务记录、投影结构化事实、分批补齐旧数据和日历元数据 | 校验并读取已提交的事实 |
| 权益、收益及目标权重回放 | 协调任务和兼容计算 | 执行账户收益计算、目标回放和回测收益计算 |
| 成本查询 | 鉴权、参数校验和 HTTP 入口 | 筛选、聚合、排序、分页 |
| 绩效和快照查询 | 选择路径，转发原生结果字节 | 读取结构化结果、重建公开响应并编码 JSON |
| 后台刷新 | 暂存和校验帧、比较源版本、原子发布、清理旧快照 | 依次计算三个区间，分批输出类型化结果行 |
| 数据库迁移和所有业务数据库写入 | 全部负责，包括成本事实的 `INSERT … SELECT` 复制 | 业务数据库严格只读 |

```text
交易与执行结果 → Python 事务写入原始 JSON ＋结构化 SQLite 事实
                                             ↓ 只读
前端 → 现有 Python HTTP 入口 → Rust 分析子进程 → JSON 结果字节
```

两边不共享 Python/Rust 对象引用。查询传小控制参数，Rust 自行读 SQLite 后返回最终响应字节；刷新则按帧交接标量行，由 Python 分批解码和暂存，完整校验后统一写入事务。Python 仍负责业务和持久化，不只是 Rust 与前端之间的转发层。

数据库文件路径由 Python 服务的实际数据库会话解析，不能由前端查询参数指定。Rust 连接以只读方式打开现有文件，不执行迁移、不创建数据库、不写入事实或运行状态。当前只支持文件型 SQLite；内存数据库和其他数据库后端使用 Python 兼容路径，显式要求原生时会报错。

查询与发布采用不同的数据交接方式：

- 绩效、成本和快照查询由 Rust 编码最终 HTTP JSON；Python 传递字节，不对响应执行 `json.loads()`、逐行模型校验或再序列化。
- 后台 `publish` 命令输出有界 JSONL 帧，Python 每次只解码当前帧，暂存后分批写入类型化结果表。JSONL 是进程间协议与临时交接形式，不会作为新快照的 `ranges` 或成本 `payload` 落库。Rust 在一个只读 SQLite 快照内依次计算 `30`、`90`、`all`，每个区间输出后释放其结果；行帧最多 256 行、编码后的行载荷最多 512 KiB，完整帧还受 1 MiB 硬限制。
- 发布流必须有唯一终止 `complete` 帧、随后 EOF，并且子进程退出成功；截断、超大帧、NaN/Infinity、尾随帧或进程失败均禁止发布。提前退出或取消会终止并回收子进程。

原生子进程结束后，其读取缓存和计算内存随进程释放。进程启动成本、SQLite 扫描、聚合状态和最终响应仍然存在，单个区间的每日结果、日历区间、绑定及回退区间仍随历史增长；此实现不承诺固定内存上限，也不声称已经完成生产负载的性能验收。

## 数据与迁移

分析数据分为源事实和已发布结果。新数据的分析读取不再依赖完整历史 JSON：

| 数据 | 结构化存储 | 写入和读取职责 |
| --- | --- | --- |
| 回放观测 | `replay_observation`、`replay_observation_symbol` | Python 在原始记录写入后维护事实；Rust 按时间读取 |
| 执行与成本源事实 | `analysis_source_execution`、`analysis_source_trade` 及品种、持仓、尝试、成交、手续费子表 | Python 在执行写入边界投影一次；Rust 分析时直接读，不再逐次解码原执行 JSON |
| 日历元数据 | `analysis_calendar_day` | Python 按渠道、日期准备小型元数据；Rust 按日期读取 |
| 已发布成本 | `performance_cost_*` 标量与明细表 | Python 将源事实复制进同一发布事务，Rust 查询已发布快照 |
| 已发布绩效结果 | `performance_result_range`、`performance_result_point`、`performance_result_skip`、`performance_result_fallback`、`performance_result_binding`、`performance_result_calendar_range`、`performance_result_event`、`performance_result_daily_cost`、`performance_result_daily_fee` | Rust 计算，Python 分批事务写入；Rust 从这些列重建公开响应 |

`performance_snapshot.result_schema_version = 2` 表示绩效结果存于上述标量表，设置使用 `backtest_weight_type`、`backtest_fee_rate` 标量列；新快照的旧 `settings`、`ranges` 列仅保留空对象兼容表结构。`cost_schema_version = 1` 表示成本明细类型完整，新快照不再向成本 `payload` 写完整 JSON。字段为空与未知、缺失身份与显式 null 等语义继续由类型列和存在标记区分，不能简单补零或强转字符串。

迁移 `0034_analysis_rust_facts` 建立类型化成本与回放基础；`0035_analysis_structured_results` 建立源事实、日历和规范化结果。源事实表初始为空，由写入边界或历史准备阶段补齐。可完整转换的旧快照升级为结果版本 2，但仍保留原 JSON 以支持降级；无法完整转换的旧快照继续标记为 0。Rust 读取版本 2 时不会选择这些残留 JSON 列。

已有执行原始输入、原始结果及审计证据继续保留原格式；目标不是删除证据，而是让分析不再反复展开它。旧数据按有界批次回填源事实。原始记录或回退权重变化时，相应事实失效后重新投影。无法无损投影的源数据标记为投影版本 0，分析队列显式选择 Python 兼容计算；这项例外在 `AXILE_ANALYSIS_MODE=required` 下也保留，不能仅凭该环境变量证明每个任务都实际由 Rust 执行。兼容计算无法完整结构化的发布可保留结果版本 0 和完整旧 JSON，不能伪造成功零结果。

日历事实记录 `available_open`、`available_closed`、`unavailable` 或 `not_required`；未注册渠道使用内部 `unregistered` 标记，公开结果仍为 unavailable 且不生成区间。Python 每次准备分析时重新评估观测覆盖日期，按 128 行批次写入；它反映当前外部日历元数据，不是不可变历史。Rust 缺少某日期时按 unavailable 处理。账户渠道变化会推进源数据版本，触发新渠道的日历准备与分析。

旧快照的版本标记为 `0` 时保留兼容能力：历史成本通过明确的 Python 旧查询读取，即使设置 `AXILE_ANALYSIS_MODE=required` 也保留这项旧数据例外；历史绩效快照可由 Rust 的旧格式读取器处理。只有这条兼容路径会读取旧快照 JSON。已选择 Rust 后的实际执行错误不会触发静默重试 Python。

已发布成本由 Python 发起 `INSERT … SELECT`，从类型化源表复制到快照表，不将源历史读成 Python 行对象或重新解析 JSON。复制时重新核对源身份、账户、投影版本和成交数量，缺失或不支持的源事实使发布失败。

Python 先把发布帧暂存到磁盘，检查完整终止标记、账户、源版本及各表行数后，才开启写入事务。计算阶段不占用 Python 写事务；写事务内再比较源版本并提交快照指针。

一次发布的全部事实、完成标记和当前快照指针必须在同一事务提交。Rust 遇到不存在、账户不匹配或过期快照时返回稳定错误；账户源版本变化时，Python 丢弃已计算但未发布的结果，不允许旧版本覆盖新历史。

部署时继续使用项目现有的迁移流程，不要从独立 Rust 程序执行迁移。首次迁移、旧数据回填、正式启用和性能比较应先在数据库副本上完成；本文中的命令不会自动修改生产数据库。

## 开发构建

需要 Rust 工具链与 C 编译环境；SQLite 通过 `rusqlite` 的 bundled 功能编译，不依赖生产主机上的 Python SQLite 扩展。Cargo.lock 随源码提供，构建使用 `--locked`。

在 Axile 子模块根目录运行：

```bash
uv run python scripts/build_native_analysis.py --timeout 180
axile/server/_native/axile-analysis --version
axile/server/_native/axile-analysis capabilities
```

首次 release 构建需要获取及编译依赖，脚本默认总计给 Cargo 180 秒，版本检查另限 20 秒。超时会失败，可在明确允许更长构建时调整 `--timeout`。服务运行时不会自动执行 Cargo。

脚本将当前主机编译出的程序先运行 `--version` 检查，再原子复制到 `axile/server/_native/`，保留可执行权限。Windows 文件名为 `axile-analysis.exe`。`--debug` 用于调试构建，`--no-build` 仅检查、复制已经生成的程序。Cargo 的 target 目录与暂存程序均不入库。

若本机配置的 `RUSTC_WRAPPER` 因缓存守护进程失效而阻塞，可仅对本次构建清除该设置后重试：

```bash
RUSTC_WRAPPER= uv run python scripts/build_native_analysis.py --timeout 180
```

## 启用与回退

| 设置 | 行为 |
| --- | --- |
| `AXILE_ANALYSIS_MODE=auto`（默认） | 找到可用程序及文件型 SQLite 时使用 Rust；未构建时保留 Python 路径 |
| `AXILE_ANALYSIS_MODE=required` | 要求原生路径；缺少程序或数据库不适用时明确失败；版本 0 的历史成本及无法投影的源数据保留上述 Python 兼容例外 |
| `AXILE_ANALYSIS_MODE=python` | 显式使用 Python 兼容路径 |
| `AXILE_ANALYSIS_BINARY=/absolute/path/axile-analysis` | 指定程序位置；路径错误明确失败 |

未指定程序时，依次寻找安装包内 `axile/server/_native/axile-analysis` 和源码目录下 `rust/analysis/target/release/axile-analysis`。设置显式程序路径意味着要求原生执行。已选择 Rust 后的运行、协议或资源限制错误不会悄悄重新执行 Python 计算。

例如，在当前项目启动方式之前设置：

```bash
AXILE_ANALYSIS_MODE=required uv run axile --host 127.0.0.1 --port 8000
```

发布到运行环境前，先在同一平台的隔离环境完成迁移与接口一致性检查。可通过 `AXILE_ANALYSIS_MODE=python` 回退执行路径；无需删除已写入的结构化事实。

| 资源设置 | 默认值 | 作用 |
| --- | --- | --- |
| `AXILE_ANALYSIS_TIMEOUT_SECONDS` | `120` | 包含排队和执行的任务期限 |
| `AXILE_ANALYSIS_MAX_CONCURRENCY` | `2` | 每个 Python 事件循环允许同时运行的原生任务数 |
| `AXILE_ANALYSIS_MAX_OUTPUT_BYTES` | `67108864` | 查询响应或发布流的总输出字节上限 |
| `AXILE_ANALYSIS_MAX_FRAME_BYTES` | `1048576` | 发布流单帧字节上限；Rust 同时限制为至多 1 MiB |

请求限制为 1 MiB，错误输出限制为 64 KiB。超时或请求取消时终止并回收子进程。多个 Python worker 的总并发需要按 worker 数一起估算，不能把上述并发设置当作整台主机的全局限制。

## Wheel 打包

先构建暂存程序，再使用项目已有的 wheel 构建流程，例如：

```bash
uv run python scripts/build_native_analysis.py --timeout 180
uv build --wheel
```

构建钩子只在暂存了当前平台的程序时打包它，并生成 `py3-none-<本机平台>` wheel；本地 Linux 构建不会自动声称 manylinux 或 musllinux 兼容。未暂存程序时仍生成纯 Python wheel，继续支持兼容路径。

这不是跨平台构建方案：需要分别在目标系统和架构构建、验证，不应把其他机器生成的程序复制过来再用本机标签打包。源码分发不携带已编译程序。正式发行前还需要按现有发布门禁检查 wheel 内容、目标系统动态库兼容性和隔离安装。该功能没有自动发布或安装到生产环境。

## 验收重点

先比较同一份数据库副本、相同查询条件下的响应语义；重点覆盖快照过期、分页游标、空值、手续费币种、跨日成交及回放缺行情。再比较整套 Python 与原生子进程的合计驻留、峰值、任务结束后的回落，以及后台分析期间账户列表的尾延迟。同时测量并发写入时的 WAL 增长、检查点推进和发布事务耗时；单次只读事务及源事实复制的工作量仍随历史增长。

只比较 Rust 进程的空闲占用、单个响应大小或单次查询耗时，不能证明整套服务已经降低内存。历史 JSON 回填和兼容路径也必须纳入测量。

已有证据分别记录在[独立回放测量](native-replay-measurement.md)与[完整发布链测量](structured-publication-measurement.md)。后者包含 Python 与 Rust 的合计采样 RSS、三轮实际事务发布和快照保留验证；仍不是完整 HTTP 服务改动前后的生产对照。
