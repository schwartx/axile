# 素材环境与复现

运行源为当前 `./axile` 公共核心子模块，源码提交 `1f89098e251187ba44b1c6f909709f434b973f50`。管理前端从该源码执行 `bun run build`，由 Axile 服务同源托管。

## 当前运行实例

- 页面：`http://127.0.0.1:8019`。
- 独立 Python 环境：`/tmp/axile-docs-20261012/.venv`。
- 配置与数据库：`/tmp/axile-docs-20261012/config.toml`、`/tmp/axile-docs-20261012/axile.db`。
- 运行日志：`/tmp/axile-docs-20261012/logs/axile.jsonl`。
- 演示账户 ID：1；组合 ID：1；自动执行关闭。
- 账户状态夹具：`offline-docs-main.json`；场景配置：`scenario.json`。两者均位于上述临时目录。

安装命令：

```bash
UV_PROJECT_ENVIRONMENT=/tmp/axile-docs-20261012/.venv   uv sync --project axile --frozen --no-dev --extra tqsdk
```

启动时在临时目录运行公共 `axile` CLI，并设置：

```text
AXILE_CONFIG_TOML=/tmp/axile-docs-20261012/config.toml
AXILE_DOCS_STATE_DIR=/tmp/axile-docs-20261012
AXILE_DOCS_OFFLINE=1
PYTHONPATH=/tmp/axile-docs-20261012/harness
```

## 夹具具体替换了什么

`harness/sitecustomize.py` 仅在 `AXILE_DOCS_OFFLINE=1` 时启用补丁。它在主进程和 spawn 子进程启动时，用 `DocumentationExecutor` 替换天勤渠道的执行器工厂。保留公共渠道描述、表单、UI、函数运行器、API、数据库迁移、审计、执行结果投影与绩效计算代码。

替身不导入天勤执行器，不连接行情或柜台，只接受 `account_mode=sim` 且用户名以 `offline-docs` 开头的配置。直接下单与撤单方法会报错；`execute()` 直接构造教学结果，**没有运行 TARGET-POS-TASK 的真实算法循环**。资产来源与结果 memory 均包含 `documentation_fixture` 标记。结果中的 0.8 秒、零滑点与零收益是夹具数字。

配置 `{"partial_symbol":"rb2701"}` 并将起始持仓重置为多 6 手、空 1 手，可生成部分成交场景。其他场景使用 `{}`。采集结束恢复普通场景并补齐目标，最终多 12 手、空 2 手；不存在外部订单。

## 重新启动当前实例

```bash
bash axile/docs/guide-materials/support/start-demo.sh /tmp/axile-docs-20261012
```

先停止同一实例的旧进程，避免端口冲突。脚本仅维护指定临时目录，默认端口 8019；需要换端口时设置 `AXILE_DOCS_PORT`。在另一目录运行会创建全新的空数据库，需按章节 03 在界面创建组合与账户。初始资金填 1,000,000，用户名使用 `offline-docs-main`，其他教学输入见章节说明。

截图使用同一个 ego-browser TaskSpace，固定浅色主题和 1440 × 1000 视口。所有原图都直接来自 `page.screenshot()`，没有粘贴假数据或修改 DOM 文本。

[运行环境与执行证据摘要](runtime-proof.json) · [全部图片清单](../manifest.json) · [采集发现](findings.md)
