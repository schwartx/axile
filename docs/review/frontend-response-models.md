# 前端 API 响应模型清理记录

基线：Axile `fix/account-record-reconciliation`，`74de07e60aeafc8eb9151e74f300c64b993c8359`，开始时工作区干净。Axon 为 `next` / `d5547fd6`，已有子模块指针差异；本任务在 `./axile` 实施，未提交父仓库 gitlink。

## 输出职责

| 原响应模型范围 | 现有输出生产处 | 清理后的职责 |
| --- | --- | --- |
| 账户及组合读取、资产页、运行态、审计、执行事件及附件、通知状态 | 对应 `*_json` 原生操作与路由中的 `Response` | 保持 JSON 字节、状态码和响应头透传，不解析重编码 |
| 创建、复制、更新及归档账户 | `account_crud._account_response` → 原生 `account_public` | 直接消费已有公开投影；运行态重试显式选择公开字段 |
| 仪表盘、未来执行、持仓对照、目标快照及换算证据 | `account_crud`、`account_execution`、`target_weight_snapshots` | 字典承接字段默认值和历史证据降级；不新增换算规则 |
| 当前委托、撤单回执、近期成交与操作失败 | `public_order`、`run_activity_command`、`account_orders` | 保留身份、排序、版本、未知状态和深拷贝；HTTP 与 SSE 复用同一公开快照 |
| 执行触发、终止、状态轮询、组合试跑 | `account_execution`、`portfolio` | 显式响应字段与默认空值，保留原状态码和异常映射 |
| 算法目录、列表、渠道能力、本机目录 | 原生扫描、`algorithms._to_public`、`capabilities`、`utils` | 去掉输出包装；保留参数模型生成 JSON Schema、稳定顺序及渠道能力公开字段 |
| 排程预览、初始化、通知试跑 | 对应路由的现有函数 | 保留默认值、日期编码和成功／失败回执 |
| 连接任务、步骤及记录表 | `ValidationService`、连接记录器、`connection_runtime` | 内部任务仍校验及更新状态，公开接口只导出任务字段和表格快照 |

仅输出类及其导出、测试模型包装已经删除。`AccountRuntimeSyncPublic` 改为 `AccountRuntimeSyncState`，保留账户内部配置对齐职责；`ValidationJob`、连接步骤、渠道描述、账户快照和输入校验模型继续存在。所有前端路由的 `response_model` 均为 `None`，结构测试同时核对请求模型仍被注册。

## 协议中的凭证边界

现有账户编辑接口会在 `connection_values` 中按渠道声明的表单字段白名单回显连接值，其中可以包含密码。保留这一现有协议，由账户凭证回显测试覆盖；完整 `account_config`、Webhook 和非白名单内部配置不回传。本次清理没有将“保持 JSON 行为”扩展为改变凭证回显策略。

## 验证

- Ruff：修改的 Python 文件格式及静态检查通过；`git diff --check` 通过。
- UI `bun run typecheck` 通过。Python 导入当前 checkout；复用 `axile/server/_native/_core.abi3.so`（`PUBLIC_HEAD=0036`），本次没有重建 Rust。
- BasedPyright：项目默认配置为 0 errors。另对整个 `axile/server` 扩大扫描，11 项错误与只读导出的原 HEAD 完全一致（逐文件和错误文本对比），未引入新增错误。
- 原生 HTTP 字节、事件及附件字段、运行态三态、通知空值：`test_native_http_read_bytes.py` 的 24 项通过。
- 执行详情、账户写入及 PATCH、连接状态：三个文件共 165 项按最多 8 项拆组执行，每组 30 秒上限；迁移测试中的字典属性访问后全部通过。
- 记录核对：18 项通过，包含取消 HTTP 等待、请求超时、继续落库、并发读取、任务身份与资产刷新结果。
- 其他定向测试覆盖账户聚合、目标快照、持仓对照、委托与撤单、账户运行态、连接操作及验证、算法发现、组合试跑、排程与补发预览、初始化、通知和渠道插件。
- 新增 `test_frontend_response_boundary.py` 检查全部路由无响应模型，保留输入模型；`test_public_response_contracts.py` 固定空目标、真实零、坏历史降级和执行状态公开字段。

浏览器验收使用仓库现有测试，测试服务隔离数据库；大部分接口沿用测试原有模拟响应，算法目录用例请求真实原生扫描 API。启动探测等待未监听端口，首次运行未进入测试；随后手动启动同一测试后端及 Vite，使用临时配置跳过重复启动和 WASM 构建。最终六个文件共 97 项，90 项通过。通知页两处定位改为匹配可见文本；委托页两处恢复状态定位按文案过滤，避免与持仓核对状态混淆。这些修改仅涉及测试。

剩余 7 项均为 `orders.e2e.ts` 的 `activity loading keeps geometry`：加载结束后表头整体上移 24px。为区分本次改动与既有问题，将原 HEAD 的 Python 源码及测试服务只读导出到隔离目录，使用同一原生扩展和未改动的前端，在独立端口 1459 / 1448 重跑；7 项全部复现相同断言失败。本次保留这些布局断言及页面实现，没有放宽几何要求。当前测试与原 HEAD 的 traces、错误输出分别位于 `/tmp/axile-response-browser-completion-*` 和 `/tmp/axile-response-browser-geometry-baseline`；因此浏览器验收并非全绿。

分项：算法设置 17、连接验证 6、Cron 设置 6、通知 16、记录核对 11 项全部通过；委托 41 项中 34 项通过，7 项为上述既有布局失败。测试后端与 Vite 均已关闭，临时启动配置已删除。

未修改 Rust 业务逻辑、数据库迁移及前端 TypeScript 协议类型；未做真实账户交易、wheel 打包、发布或部署。
