# SQLite 结构化事实与 Rust 分析

这条实现保留 SQLite 作为事实存储，由 Python 事务写入，Rust 只读历史事实并执行收益回放、成本聚合和查询。没有引入 DuckDB、Polars 或 Parquet 存储，也没有替换整个后端的 Pydantic。

Python 继续处理认证、参数校验、任务协调、数据库迁移和快照发布。Rust 接收账户、快照和查询条件等小请求，从同一个 SQLite 文件直接读取结构化字段；历史数据不先转换成 Python ORM 对象或大字典再传入 Rust。

## 运行边界

原生部分是按任务启动的独立可执行程序 `axile-analysis`，通过 stdin/stdout 交换控制请求和 JSON 结果，不监听额外 HTTP 端口。已有 Python HTTP 入口保留认证和接口路径；适用的查询直接返回 Rust 编码的响应字节，避免再次构造完整 Pydantic 响应对象。后台发布协调仍由 Python 负责。

```text
交易与执行结果 → Python 事务写入结构化 SQLite 事实
                                      ↓ 只读
前端 → 现有 HTTP 入口 → Rust 分析子进程 → JSON 结果字节
```

数据库文件路径由 Python 服务的实际数据库会话解析，不能由前端查询参数指定。Rust 连接以只读方式打开现有文件，不执行迁移、不创建数据库、不写入事实或运行状态。当前只支持文件型 SQLite；内存数据库和其他数据库后端使用 Python 兼容路径，显式要求原生时会报错。

原生子进程结束后，其读取缓存和计算内存随进程释放。进程启动成本、SQLite 扫描、聚合状态和最终响应仍然存在；此实现不承诺固定内存上限，也不声称已经完成生产负载的性能验收。

## 数据与迁移

迁移 `0034_analysis_rust_facts` 为已有成本投影补充类型化字段，并创建区间、品种、持仓、尝试、成交及手续费明细表。`performance_snapshot.cost_schema_version = 1` 表示该快照的结构化成本事实完整，可以供 Rust 查询。迁移按有界批次回填既有成本投影；无法完整转换的旧数据不会标记为完成。

原有 `payload` 等 JSON 列暂时保留兼容和审计用途。Rust 的常规成本读取使用显式字段与子表，不依赖这些大 JSON。收益回放使用 `replay_observation`、`replay_observation_symbol` 结构化观测。Python 在新增记录或更新分析输入时维护对应事实；既有历史由分析准备阶段分批回填。

旧快照若包含不能无损转换为声明类型的值，会保留 `cost_schema_version = 0` 并明确选择原 Python 成本查询。新发布遇到同类历史时也先原子回滚全部结构化成本行，再保留兼容快照；不会把未知值强转为字符串或零。这是按数据版本确定的兼容路径，不是在任意原生执行错误后重试 Python。

当前迁移并未消除系统内全部 JSON：原始执行输入、审计证据和兼容快照继续保留。Python 仍负责准备、投影并发布成本事实，兼容路径也仍然存在；Rust 优化覆盖结构化事实的热查询和收益回放。历史回填、Python 成本投影及快照发布期间的内存仍须单独计量，不能把原生读取已启用等同于所有历史对象已经退出 Python。

一次发布的事实与完成标记必须在同一事务提交。Rust 遇到不存在、账户不匹配、过期或不支持的快照时返回稳定错误，不能把缺失事实误当作零收益或零成本。账户源数据版本变化时，Python 仍负责阻止旧版本结果发布。

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
| `AXILE_ANALYSIS_MODE=required` | 要求原生路径；缺少程序或数据库不适用时明确失败 |
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
| `AXILE_ANALYSIS_MAX_OUTPUT_BYTES` | `67108864` | 单次 stdout 响应字节上限 |

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

先比较同一份数据库副本、相同查询条件下的响应语义；重点覆盖快照过期、分页游标、空值、手续费币种、跨日成交及回放缺行情。再比较整套 Python 与原生子进程的合计驻留、峰值、任务结束后的回落，以及后台分析期间账户列表的尾延迟。

只比较 Rust 进程的空闲占用、单个响应大小或单次查询耗时，不能证明整套服务已经降低内存。历史 JSON 回填和兼容路径也必须纳入测量。
