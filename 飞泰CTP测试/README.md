# 飞泰 CTP 联机测试脚本

通过运行中的 Axile REST API 执行 CTP 开平撤、统计对账、额度阈值、非法输入和暂停时序场景。运行入口直接联机，真实交易由 Axile 执行器完成；测试替身只用于开发回归。

本目录只提供通用脚本、回归测试和使用说明，不包含实际账户配置、联机结果或验收记录。

## 安装与配置

在 Axile 服务及 worker 使用的同一 Python 环境中安装本目录，然后重启 Axile，使服务加载 `FT-CTP-SCENARIO` 算法入口：

```bash
uv pip install --python .venv/bin/python --no-deps -e ./axile/飞泰CTP测试
cp ./axile/飞泰CTP测试/config.example.json ./axile/飞泰CTP测试/config.local.json
```

修改本地配置中的 `database`、`account_name`、`api_url`。数据库路径和结果路径相对配置文件解析；账户名称精确匹配数据库里的一个 CTP 账户。配置只选择现有账户，密码、认证码等凭据从该数据库只读取得，再交给 Axile，不另建 SDK 通道。

`symbol` 留空时，交易命令必须提供 `--symbol` 或 `--set symbol=...`；也可以在本地配置中填写本次明确选定的测试合约。不得直接把文档中的合约占位符当作真实合约。

所有命令必须传 `--config`。鉴权从 `AXILE_API_PASSWORD` 或 `AXON_API_PASSWORD` 环境变量读取。本地配置、运行结果、构建和缓存均不提交。

## 连接检查

以下命令从工作区根目录运行：

```bash
uv run python ./axile/飞泰CTP测试/run.py inspect --config ./axile/飞泰CTP测试/config.local.json
uv run python ./axile/飞泰CTP测试/run.py connect --config ./axile/飞泰CTP测试/config.local.json --batch connect-001
```

`inspect` 只读核对凭据来源，不输出凭据值；`connect` 经 Axile 配置所选账户并建立真实 CTP 连接，不发交易委托。连接未达到 `ready` 时不执行交易场景。

## 交易场景

**`run` 会通过 Axile 发起实际交易。** 将下例 `TEST_CONTRACT` 替换为本次认可的真实合约。账户自动调度应已暂停，无剩余补发、活跃执行或存量挂单。

```bash
uv run python ./axile/飞泰CTP测试/run.py run --config ./axile/飞泰CTP测试/config.local.json --symbol TEST_CONTRACT --batch basic-001
uv run python ./axile/飞泰CTP测试/run.py run --config ./axile/飞泰CTP测试/config.local.json --symbol TEST_CONTRACT --batch cancel-001 --scenarios cancel
uv run python ./axile/飞泰CTP测试/run.py run --config ./axile/飞泰CTP测试/config.local.json --symbol TEST_CONTRACT --batch stats-001 --scenarios stats --set repetitions=3
```

默认运行 `open,close,cancel,stats`。`--scenarios` 可指定逗号分隔的场景；前一步未通过，后续交易停止，仍保留失败证据。

默认每笔最多 1 手、总测试敞口最多 1 手、批次请求预算 12（上限 100）。其他合约持仓按原数量目标保留并核对。不追价补单，不自动重试报撤请求；未知提交不得再次执行同一批次。

开仓使用目标数量，重复目标不会追加开仓。`--set side=short` 可验证空头；`--set close_offset=today/yesterday/auto` 选择平仓方式，数量不足不会替换平仓标志。撤单仅针对本批次明确拥有的原生订单，并核对实际终态；接口受理、任务成功或订单消失不能单独证明成交或撤单成功。

## 阈值、非法输入与柜台拒绝

```bash
uv run python ./axile/飞泰CTP测试/run.py run --config ./axile/飞泰CTP测试/config.local.json --symbol TEST_CONTRACT --batch threshold-001 --scenarios threshold --set daily_limit=6 --set warning_percent=50 --set threshold=3
uv run python ./axile/飞泰CTP测试/run.py run --config ./axile/飞泰CTP测试/config.local.json --symbol TEST_CONTRACT --batch invalid-001 --scenarios invalid --set invalid_case=price
```

`invalid_case` 支持 `price/volume/unknown`。未知合约通过后端 `POST /instrument-catalog/query` 的精确元数据查询验证；后端须包含该接口。负查询不运行组合函数，不将规划跳过记成委托校验通过；只有结构化 `CATALOG.NOT_FOUND` 可作为对应检查命中的依据。

阈值必须满足 `threshold=ceil(daily_limit*warning_percent/100)`。不重置已有计数、不关闭或放宽保护。报单统计采用 `place_order`，撤单只采用原生 `cancel_order_ctp`，不叠加外层撤单；本地放行、柜台受理与成交分别核对。过期、跨日、分页缺失等情况标记未知。

`reject` 场景要求填写柜台认可的 `broker_condition`、`reject_case`、真实期望 `expected_error_id` 及手数约束；`reject_case` 支持 `insufficient_funds/no_position/market_state`。本地保护先阻断时如实记录条件不足。原生返回码、异步错误码和原文分别保存，未知错误码不猜测含义。

## 暂停与恢复

```bash
uv run python ./axile/飞泰CTP测试/run.py run --config ./axile/飞泰CTP测试/config.local.json --symbol TEST_CONTRACT --batch pause-001 --scenarios pause --set pause_checkpoint=after_send --set terminate_mode=cancel_pending
```

`pause_point` 支持 `executing/quota/queued/scheduled/supplement/idle/restart`。执行中可用 `pause_checkpoint=before_send/after_send`；`pause_action` 支持 `terminate/pause_auto/cancel_supplement`。终止默认 `graceful`，也可指定 `cancel_pending`。暂停自动调度不禁止手动执行，不伪造统一暂停确认。

定时或补发场景需设置足够的 `observe_seconds` 和 `timeout_seconds`，覆盖实际触发窗口；默认短观察窗口不能证明后续没有触发。`restart` 需要另一终端重启服务并确认实例变化。受理、实际结束和观察结果分别保存。无法确认任务已结束时不恢复仍可能使用中的配置。

任务 `TERMINATED` 不代表柜台订单已撤销。残留按账户、合约及原生订单身份核对，不使用一键清仓或清理无关订单。

## 证据与只读补收

结果写入 `results/<batch>/`，包括请求、执行状态、分页事件和附件、逐笔核对、控制审计、产品分享 ZIP、清单及最终残留。脚本核对 ZIP manifest；材料或日志缺口进入索引，不把未知记为通过。

实际执行结束后可以补收，不重新发单：

```bash
uv run python ./axile/飞泰CTP测试/run.py collect --config ./axile/飞泰CTP测试/config.local.json --execution-id EXECUTION_ID --batch collect-001
```

采集前必须核对 execution 属于所选账户且已进入终态。证据字段统一脱敏；长凭据可在文字中替换，短凭据只按敏感字段或带字段名的文字处理，以保留订单身份和时间信息。分享前仍需检查非结构化原文，不把生成结果直接提交到源码仓库。

结果状态包括 `passed/failed/not_executed/insufficient_conditions/unknown`。退出码 0 仅表示场景通过且没有已知材料缺口，其他结果返回 2。

## 开发验证与构建

```bash
uv run pytest ./axile/飞泰CTP测试/tests -q
uv run ruff check --config ./axile/飞泰CTP测试/pyproject.toml ./axile/飞泰CTP测试
uv build --out-dir ./axile/飞泰CTP测试/dist ./axile/飞泰CTP测试
```

测试替身不进入运行包；回归测试不代替真实柜台验收。产品问题及修复进展见 #108—#112，本目录不附实际联机证据。
