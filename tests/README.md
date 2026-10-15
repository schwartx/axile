# 测试指南

测试按运行条件分为以下目录：

- `tests/unit/`：不访问真实外部服务的单元测试。
- `tests/contract/`：扫描仓库或验证公共边界的契约测试。
- `tests/integration/`：使用真实数据库、线程、IPC 或子进程的跨模块测试。
- `tests/live/`：需要显式开关及真实环境的联机测试。

安装开发依赖和 CTP 测试依赖：

```bash
uv sync --group dev --extra ctp
```

离线测试套件包含 CTP 渠道用例；`openctp-ctp` 是 CTP 的可选运行时依赖，
因此执行完整测试套件时必须安装 `ctp` extra。未安装该 extra 的环境仅适合
不运行 CTP 测试的开发工作。

运行离线测试与覆盖率门禁：

```bash
uv run pytest tests/ -v \
  --ignore=tests/live \
  --cov=axile --cov-report=xml --cov-report=term-missing --cov-fail-under=69
```

测试启动时会在系统临时目录生成独立的 ``config.toml``、SQLite 数据库和日志目录，
并通过 ``AXILE_CONFIG_TOML`` 传递给测试启动的子进程。测试不得读取或写入工作目录中的
``axile.db``；需要数据库的用例应使用这套会话级隔离环境或自己的 ``tmp_path``。

按反馈速度分层运行：

```bash
# 快速反馈：纯单元测试和仓库契约
uv run pytest tests/ -m "unit or contract" --ignore=tests/live

# 使用真实数据库、IPC 或子进程的离线集成测试
uv run pytest tests/integration/ -m integration

# 全部离线门禁（CI / 提交前）
uv run pytest tests/ --ignore=tests/live
```

运行单个文件或测试：

```bash
uv run pytest tests/unit/channels/test_registry.py -v
uv run pytest tests/unit/channels/test_registry.py::test_duplicate_channel_registration_fails_clearly -v
```

联机测试默认跳过。需要运行时，将 `tests/live.config.example.toml` 复制为已被
`.gitignore` 忽略的 `tests/live.config.toml`，填写对应 section，并设置测试文件说明的
`RUN_LIVE_*` 环境变量。

新增测试应保持确定性，不依赖执行顺序、真实时钟或公网服务。渠道插件自身的实现测试
应放在对应插件项目，公共核心只验证开放注册协议与通用行为。

## 维护约定

- 按业务行为确定测试归属。模型、helper、服务和路由覆盖同一普通场景时，优先保留能验证完整结果的
  用例；资金、数量、开平仓、撤单确认、成交去重、恢复隔离和事务一致性仍需独立覆盖失败边界。
- 普通字段赋值、日志字符串拼法、内部方法调用顺序和第三方校验器的重复输入不作为独立回归目标。
  序列化只保留跨进程、数据库或公共 API 的必要契约。
- 参数化的每组输入应保护不同的业务边界。相同分支上的等价普通值可精简；不同渠道的交易风险、
  不同故障阶段及并发时序不能仅因覆盖相同行而合并。不得把参数表搬进循环来制造用例数量下降。
- 精简时对比完整离线回归与覆盖率，复核关键安全路径的覆盖损失；覆盖率只能辅助筛选，不能单独证明
  两个行为测试等价。保留现有 CI 全量执行范围和覆盖率门槛。
- 可跨文件复用的 fake、builder 和 clock 放在 `tests/fixtures/`；测试文件之间不得互相导入。
- 文件名按被验证的业务行为命名，不使用 `test_issue_fixes.py` 一类长期收容回归的名称。
- 一旦测试需要真实 SQLite、线程、IPC 或子进程，就放入 `tests/integration/` 并标记为 `slow`。
- UI 纯函数可直接单测，组件输出可用服务端渲染验证；源码文本扫描只用于仓库级边界规则，
  不用于代替组件交互或浏览器布局验证。
