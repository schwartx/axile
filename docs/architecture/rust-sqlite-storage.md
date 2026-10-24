# Rust SQLite 存储边界

第一方生产代码的 SQLite 查询、业务写入、初始化和迁移由 Rust 负责。Python 保留 HTTP 鉴权与参数校验、普通 Pydantic 数据模型、策略、渠道 SDK、Clock 和调度编排。`db/models/` 中的模型保留业务字段、默认值和验证规则，不持有 ORM 会话。

## 调用与事务

Python 通过 `native_operation()` 或 `native_operation_sync()` 调用一个完整业务阶段，每个阶段只跨一次 FFI 边界，传入数据库路径和请求。Rust 返回必要的 DTO、回执，纯 HTTP 查询直接返回最终 JSON 字节，避免结果在两种语言之间反复拆装。数据库路径来自服务配置；显式路径用于已捕获数据库的记录器、探测或测试边界，不由前端查询参数指定。原生错误保留 `(code, message)`，由所属 API 映射为既有错误响应。

PyO3 入口在持有 GIL 时取得请求和路径，等待数据库、查询、计算和编码时释放 GIL。异步入口在线程中执行，避免 SQLite 等待阻塞事件循环。Python 只保留少量按领域命名的读取和模型辅助函数，例如 `repositories.py`；不要增加 Session 兼容层、通用 SQL 代理或 Python 存储回退。

Rust 业务入口拥有连接和事务，内部辅助函数接收同一个 `&Connection`，不另开连接或自行提交。需要一起成功的写入包括：

- 账户变更及其绑定、运行状态、连接重置和补发取消；组合保存及目标快照。
- 执行终态主记录、输入引用、全部结果事实、资产历史和摘要引用。执行前输入与执行后资产分别是独立持久化阶段。
- 执行意图、补发步骤及通知 outbox。SDK 发送前必须提交 `pending`，发送成功后的记录故障不能把订单变成可重试发送。
- 账户控制批次整体提交；依赖已落库事实的预警另开后续事务。执行事件审计批次允许逐行 SAVEPOINT，保留单行失败隔离。

业务写连接使用 WAL、`synchronous=FULL` 和 30 秒 busy timeout，写事务使用 `BEGIN IMMEDIATE`。事务只包住本阶段数据库工作，不能持锁等待策略、worker 或网络。业务读写只打开已有文件，路径错误不能悄悄创建空库。业务时间戳由 Python Clock 生成并传入 Rust，保持仿真时钟可替换。

连接复用由 Rust 统一管理，所有数据库路径合计最多缓存 8 条空闲连接；读连接、写连接和不同 `foreign_keys` 模式分别复用。Python 的 `analysis_store()` 按当前数据库路径缓存一个只保存路径的 `Store` 句柄，避免重复调用原生构造器；Python 不缓存 SQLite 连接。借用连接时核对数据库文件身份，文件替换后不沿用旧连接；迁移会使对应路径的连接缓存失效。Windows 替换或删除数据库文件前，需先关闭服务或调用 `close_database` 释放缓存句柄。

连接归还前重置未结束的事务；不能重置，或留有附加数据库、临时对象的连接不再复用。分析计算不占用全局独占写入租约，写入互斥由实际的 `BEGIN IMMEDIATE` 事务保证。SDK 日历评估仍在数据库租约和事务之外完成。服务关闭时先停止后台任务、调度与 worker，再通过 `close_database` 释放对应数据库的空闲连接，避免句柄和 WAL 文件继续占用。

异步调用被取消后，桥接层会等待原生线程结束，再传播取消，调用方的锁也须保留到这一步。取消、回执丢失或调用进程退出都不能证明事务已回滚；恢复依据数据库已提交状态和本次操作身份，不能仅因缺少回执而重发。

## 迁移与维护

Rust 的迁移引擎负责建库、公共 `0001`—`0039` 历史数据转换、升级和降级。Axon 将私有规则作为数据一次传入，Rust 在同一事务中完成公共链和私有 `axon_0001` 链，全部成功后提交。继续使用 `alembic_version`、`axon_alembic_version` 及原修订号，不调用 Python Alembic 回调；`ANALYSIS_FACT_SCHEMA=40` 是分析事实版本，不是迁移修订号。`current` 和 `check` 只读，数据库不存在时保持不存在。降级保留证据可表示性和字段恢复冲突检查，不能为完成降级丢弃证据。

测试 oracle、SQLite 夹具以及历史 Alembic/SQLAlchemy 对照留在开发测试侧，不进入生产存储路径。源码边界检查见 `tests/contract/test_native_storage_source_boundary.py`，桥接取消和真实事务契约分别见 `tests/unit/test_native_store_bridge.py`、`tests/integration/server/test_native_storage_transactions.py`。维护目标是 Python 更薄、存储边界统一、行为容易核对；总行数可以随 Rust 实现和必要验证增加。

入口代码见 [`native_store.py`](../../axile/server/native_store.py)、[`python.rs`](../../rust/storage/src/python.rs) 和 [`migrations.rs`](../../rust/storage/src/migrations.rs)。结构化事实的表结构与证据语义见[执行业务事实的结构化存储](../performance/business-facts.md)。

## Crate 命名与拆分评估

Rust 实现在 `rust/storage/`，Cargo 包名为 `axile-storage`。Python 导入入口仍是
`axile.server._native._core`；构建脚本仍使用 `scripts/build_native_analysis.py`。
crate 同时承担 SQLite 业务存储和分析，`analysis` 不再适合作为整个 crate 的名字。

当前保留单 crate。按现有源码依赖，拆分边界如下：

| 候选 | 当前依赖与调用方 | 结论 |
| --- | --- | --- |
| `replay.rs` | 只依赖标准库和 `serde::Serialize`；`performance.rs` 消费账本，`fact_observation.rs` 构造 `Observation` | 可直接提取纯计算 crate；现有 14 个单元测试可随迁 |
| `performance.rs` | 依赖 SQLite、事实扫描与完整性校验、数据库日历 | 不能直接作为纯计算 crate 提取；需先分离数据库读取和计算 |
| `costs.rs` | SQL 聚合、成交事实类型、快照时间、迁移兼容逻辑和发布范围读取 | 暂留存储层；整体移入计算 crate 会反向引入存储依赖 |
| `publication.rs` 与 `native_publication/` | 读取一致性快照、流式结果暂存、版本校验及原子发布 | 作为一个存储流程保留，避免跨 crate 分散事务职责 |
| `python.rs` | 直接调用多个存储领域入口 | 技术上可独立绑定 crate，但目前会扩大存储 API 的公开面，收益不足 |

若出现独立计算调用方，第一步仅提取 `replay.rs` 到 `axile-analytics`，依赖方向为
`axile-storage -> axile-analytics -> serde`。账本的 `Observation`、`ReplayClose`、
`ReplayLedger` 和引擎版本由计算 crate 持有，事实读取器继续留在存储层构造输入。
先保持现有输入语义，不增加通用数据源接口或额外 DTO 转换。验收包括随迁账本测试、
收益分析回归和发布集成测试。

当前账本只有一个生产计算消费模块，也没有独立发布需求；因此本次只改名，
不增加第二个 crate。这是一份源码依赖评估，尚未通过试拆编译验证。
