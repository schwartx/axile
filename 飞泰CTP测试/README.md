# 飞泰 CTP 联机测试脚本

通过正在运行的 Axile REST API 操控宏源测试账户，完成开仓、平仓、挂单撤销、统计对账、额度预警、异常输入和暂停时序验证。运行入口直接联机，没有模拟运行模式，也不需要 `--live`。

凭据从 `../../axile.db`（即当前工作区根目录的 `./axile.db`） 中精确匹配名称为“宏源测试”的 CTP 账户，运行时交给 Axile；不会另建 SDK 下单通道。密码、认证码等不写入配置、仓库或分享结果。

## 当前环境和安装

当前开发服务：`http://127.0.0.1:1419/api/v1`。数据库账户 ID 自动查找，不硬编码。配置样例为 `config.example.json`，可复制为 `config.local.json`。其中数据库和结果路径相对配置文件解析。

在 Axile 服务及 worker 使用的同一 Python 环境安装算法插件：

```bash
uv pip install --python .venv/bin/python --no-deps -e ./axile/飞泰CTP测试
```

本次目录迁移已更新可编辑安装并重启服务。重新安装或修改算法实现后，需重启 Axile，让常驻进程及后续 worker 加载最新代码。

服务重启后 `/api/v1/algorithms` 应包含 `FT-CTP-SCENARIO`。算法通过安装包的 `axile.algorithms` 入口自动加载，实际实现全部在本目录。独立部署可安装 `dist/feitai_ctp_tests-0.1.2-py3-none-any.whl`，依赖公共 `axile[ctp]>=0.19.1`。

鉴权读取 `AXILE_API_PASSWORD` 或 `AXON_API_PASSWORD`。其他服务地址通过 `--set api_url=http://127.0.0.1:8000/api/v1` 或配置文件指定。

## 连接与账户检查

以下命令从工作区根目录运行：

```bash
# 只读检查本地凭据来源，不输出凭据值
uv run python ./axile/飞泰CTP测试/run.py inspect

# 经 Axile 写入所选凭据并建立实际 CTP 连接，不发交易委托
uv run python ./axile/飞泰CTP测试/run.py connect --batch connect-001
```

`connect` 保存实际 API 请求、连接结果和错误。`run` 也会检查服务算法注册及账户连接；连接没有达到 `ready` 就停止，并把交易场景标记为未执行。

## 开平撤和统计

**下列命令会通过 Axile 执行实际交易动作。** 合约 `rb2701` 是配置样例，运行时按本次测试合约修改。默认 1 手、单笔最多 1 手、总敞口最多 1 手，账户自动调度必须关闭、无剩余补发、无存量挂单；其他合约持仓保持原数量目标，并在场景结束后自动核对。

```bash
# 一次完整开平撤及三轮报撤对账
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch basic-001

# 各场景独立运行
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch open-001 --scenarios open
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch close-001 --scenarios close
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch cancel-001 --scenarios cancel
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch stats-001 --scenarios stats --set repetitions=3
```

开仓使用目标手数，重复同一目标不会追加 1 手；平仓要求恰有约定方向、数量的测试合约持仓，目标归零。`--set side=short` 验证空头；`--set close_offset=today/yesterday/auto` 选择平今、平昨或当前产品的平仓计划，数量不足不替换标志。

撤单使用合法、偏离盘口的限价单，确认原生订单身份和挂起回报后，只撤本次拥有的订单并等待实际终态。实际成交、部分成交、拒绝、超时和残留均保存。接口受理、任务成功或订单消失不能单独证明成交／撤单成功。

每个批次 `max_requests` 默认 12，最多 100；每笔可能报单预留一次风险撤单，今昨拆单分别预留。脚本不追价补单、不自动重试报撤请求。场景可用逗号组合，前一步未通过时后续交易停止，仍收集证据。

## 阈值和异常

```bash
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch threshold-001 --scenarios threshold \
  --set daily_limit=6 --set warning_percent=50 --set threshold=3

uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch bad-price-001 --scenarios invalid --set invalid_case=price
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch bad-volume-001 --scenarios invalid --set invalid_case=volume
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch bad-symbol-001 --scenarios invalid --set invalid_case=unknown
```

统计记录来源、窗口、起止值、增量和逐笔结果。报单采用 `place_order`，撤单合计只采用原生 `cancel_order_ctp`，不再叠加外层 `cancel_order`。这是本地风控放行口径，柜台受理／成交另外核对。快照过期、跨日、实例变化和分页不完整时标记未知。

阈值满足 `threshold=ceil(daily_limit*warning_percent/100)`，保留预警线前、到值、超过的快照和实际保护行为。不重置已有计数、不放宽保护。脚本提示标记 `source=script`，产品预警使用实际审计 `quota_warning`，通知送达另外核对。

非法价位构造半 tick 偏差；超量直接走执行器单笔接口，避免高层拆单。未知合约经现有 `Context.get_quote` 精确命中 CTP 元数据校验，并明确记录阶段，不把规划跳过说成直接委托校验通过。

柜台拒绝先填写飞泰认可的条件、真实期望错误码和手数约束：

```bash
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch reject-001 --scenarios reject \
  --set reject_case=no_position --set broker_condition='本次认可的账户、市场状态与构造方法' \
  --set expected_error_id=30
```

`30` 仅演示参数格式，需改为认可的实际错误码。`reject_case` 支持 `insufficient_funds/no_position/market_state`。本地保护先阻断时如实记录条件不足，不关闭保护。同步 `return_code`、异步 `ErrorID/ErrorMsg`、原始文字、已有中文解释及请求／订单关联分别保存，未知码原样保留。

## 暂停时序

```bash
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch pause-001 --scenarios pause
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch quota-001 --scenarios pause --set pause_point=quota
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch queued-001 --scenarios pause --set pause_point=queued
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch scheduled-001 --scenarios pause \
  --set pause_point=scheduled --set pause_action=pause_auto --set observe_seconds=65
uv run python ./axile/飞泰CTP测试/run.py run --symbol rb2701 --batch supplement-001 --scenarios pause \
  --set pause_point=supplement --set pause_action=cancel_supplement --set timeout_seconds=80 --set observe_seconds=65
```

`pause_point` 支持执行中、等待额度、排队、定时前、补发前、idle、restart。执行中可以 `pause_checkpoint=before_send/after_send` 定位发送边界。排队场景优先在首次任务 QUEUED 时终止；若已运行，尝试形成第二个排队任务。未形成、被合并或 409 时记录实际行为，异常收尾终止本批次已知任务。

`pause_action=terminate` 默认 graceful，另可 `terminate_mode=cancel_pending` 核对产品对应撤单范围；`pause_auto` 只暂停自动调度；`cancel_supplement` 沿用当前 terminate 的补发取消语义。补发场景安排一次基础触发，避免混入下一轮新组。

受理、实际结束和后续观察分开保存。暂停自动调度不禁止手动执行，不伪造统一暂停 ACK。恢复原配置另行记录。`restart` 观察窗口内由另一终端按现有方式重启服务，通过监控实例 ID 或本地 API 监听进程 PID／创建时间确认重启，无法证明实例切换不算通过。

## 结果与失败处理

结果在 `results/<batch>/`：摘要、场景／批次索引、请求日志、处理后的目标、执行状态、完整分页事件和附件、控制审计、逐笔 CSV、产品证据 ZIP、SHA256 清单和最终残留。

产品 ZIP 调用现有分享导出并核验 manifest，覆盖和日志缺口进入索引。实际版本以服务 exporter／environment_snapshot 为准。结果为 `passed/failed/not_executed/insufficient_conditions/unknown`；退出码 0 表示场景通过且材料无已知缺口，其他结果返回 2。

已存在批次禁止再次发起交易。未知提交或超时后先定位实际 execution，再只读补收：

```bash
uv run python ./axile/飞泰CTP测试/run.py collect --execution-id <execution_id> --batch collect-001
```

只在实际结束后恢复原组合、算法、杠杆、精度和规则。无法确认的提交保留未知，不覆盖仍可能在使用的配置。残留按账户、合约和原生订单身份人工核对；不会清理无关订单或持仓，不使用一键清仓。

## 开发回归与构建

脚本回归替身在 `tests/support.py` 中；`diagnostics/` 另提供核心问题的独立表征探针。运行包和 wheel 不提供模拟交易入口。这些验证不代替实际柜台结果。

```bash
uv run pytest ./axile/飞泰CTP测试/tests -q
uv run ruff check --config ./axile/飞泰CTP测试/pyproject.toml ./axile/飞泰CTP测试
uv build --offline --no-sources --out-dir ./axile/飞泰CTP测试/dist ./axile/飞泰CTP测试
```

构建命令的 `--offline` 仅指构建工具不下载依赖，与脚本交易运行无关。正式柜台对照、`.dat` 取证和飞泰验收沿用 #87/#88/#90 的分工。

本轮联机验收结论与未通过项见 [验收报告](验收报告.md)。发送后终止的强制中断与残留风险尚未消除；脚本会记录未知和残留，不能把任务 TERMINATED 当作撤单成功。

五项问题的代码归因、独立复现、解决方案及验收要求见 [问题排查与解决方案](问题排查与解决方案.md)，探针命令见 [diagnostics/README.md](diagnostics/README.md)。
