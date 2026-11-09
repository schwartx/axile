export interface NotificationField { name: string; type: string; desc: string }

export const NOTIFICATION_FIELDS: NotificationField[] = [
  { name: 'event_id', type: 'str | None', desc: '由 execution ID 生成的事件标识；试跑样例可能没有 ID。' },
  { name: 'account', type: 'dict', desc: '账户 ID、名称、市场、交易渠道等公开配置；不包含连接凭据。' },
  { name: 'event_type', type: 'str', desc: 'execution.finished 或 supplement.cancelled。' },
  { name: 'execution', type: 'dict | None', desc: '本次执行的 ID、类型、触发来源、状态、结论、错误与通知时间。' },
  { name: 'strategy', type: 'dict', desc: '算法、品种算法和交易规则等配置；疑似凭据字段会被移除。' },
  { name: 'assets', type: 'dict', desc: '总资产、可用资金、持仓市值、币种、来源和快照时间；不可用时金额为 None。' },
  { name: 'targets', type: 'dict', desc: 'current、previous 和 target_volume，分别表示本次目标、上次目标和目标数量。' },
  { name: 'positions', type: 'list[dict] | None', desc: '持仓及目标数量、目标权重；资产不可用时为 None。' },
  { name: 'orders / trades', type: 'list[dict]', desc: '本次执行的订单和成交；列表可能为空。' },
  { name: 'symbols', type: 'list[dict]', desc: '各品种的状态、结论、原因、目标数量与订单成交计数。' },
  { name: 'summary', type: 'dict', desc: '品种、持仓、订单、成交、成功与失败品种的计数。' },
  { name: 'default_feishu_variables', type: 'dict', desc: '默认飞书卡片的模板变量，可直接修改后发送。' },
]

export const EXECUTION_FIELDS: NotificationField[] = [
  { name: 'execution.is_test', type: 'bool', desc: '试跑为 True；真实执行通知为 False。' },
  { name: 'execution.id', type: 'str | None', desc: '本次 execution ID；样例可能没有。' },
  { name: 'execution.kind / trigger_source', type: 'str | None', desc: '执行类型和触发来源；样例或缺少审计信息时可能为空。' },
  { name: 'execution.notified_at', type: 'str', desc: '通知上下文构造时间，格式为 YYYY-MM-DD HH:MM:SS。' },
  { name: 'execution.execution_time', type: 'float', desc: '本次交易执行耗时，单位为秒。' },
  { name: 'execution.outcome_reason', type: 'str | None', desc: '执行结论的具体说明。' },
  { name: 'execution.channel_type', type: 'str', desc: '本次执行的交易渠道。' },
  { name: 'execution.status / success', type: 'str / bool', desc: '执行状态和成功标记。' },
  { name: 'execution.outcome / reason_code', type: 'str / str | None', desc: '执行结论和机器可读原因码。' },
  { name: 'execution.error', type: 'str | None', desc: '执行错误；没有错误时为空。' },
  { name: 'summary.trade_count', type: 'int', desc: '本次执行的成交条数。' },
]

export const NOTIFY_CODE = `import json
import os
from urllib import request
from axile.common.notification_context import AccountNotificationContext


def notify(context: AccountNotificationContext) -> None:
    # 试跑执行当前草稿；如不想发送真实消息，先检查此标记。
    execution = context["execution"]
    if context.get("is_test") or (execution is not None and execution["is_test"]):
        return

    webhook = os.environ["AXILE_NOTIFY_WEBHOOK"]
    message = {
        "account": context["account"].get("name"),
        "status": execution["status"] if execution is not None else "supplement.cancelled",
        "trades": context["summary"]["trade_count"],
    }
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    req = request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=5) as response:
        response.read()`

export const ASYNC_NOTIFY_CODE = `import os

import aiohttp
from axile.common.notification_context import AccountNotificationContext


async def notify(context: AccountNotificationContext) -> None:
    execution = context["execution"]
    if context.get("is_test") or (execution is not None and execution["is_test"]):
        return

    webhook = os.environ["AXILE_NOTIFY_WEBHOOK"]
    message = {"account": context["account"].get("name"), "status": execution["status"] if execution is not None else "supplement.cancelled"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        async with session.post(webhook, json=message) as response:
            response.raise_for_status()
            await response.read()`

export const CONTRACT_RULES = [
  '入口支持同步 `def notify(context)` 和异步 `async def notify(context)`，必须且只能接收一个位置参数。',
  '调用结果是 awaitable 时等待其完成；普通返回值被忽略。不支持生成器或异步生成器。',
  '异步入口必须自行 await 后台任务；入口结束后，未完成任务会被取消。',
  '每个账户最多保存一个通知函数；源码为空时不通知。默认飞书通知也是一个可编辑函数。',
  '默认通知通过 AXILE_ACCOUNT_FEISHU_KEY 读取账户飞书 Webhook。保存自定义函数会清除该 Webhook，自定义函数需自行配置通知渠道和凭据。',
  '函数在独立进程中运行，最长 15 秒。失败只记录通知错误，不改变交易结果，也不会自动补发默认飞书卡片。',
]

export const TEST_RULES = [
  '点击“试跑函数”会执行编辑器中的当前草稿，无需先保存。试跑可能真的向外发送消息。',
  '两类试跑的 `context["is_test"]` 均为 `True`；执行完成样例同时保留 `execution.is_test`。取消样例的 execution 为 None。',
  '试跑成功只表示函数在样例上下文中执行成功；不能保证真实事件的字段都有值，或外部服务始终可用。',
  '保存后，后续账户执行通知使用已保存的唯一函数源码；清空并保存即可关闭。',
]

export const FAILURE_RULES = [
  'KeyError / TypeError：先检查字段是否存在、值是否为 `None`，尤其是资产、持仓和 execution ID。',
  '通知函数执行超时：外部请求设置短于 15 秒的超时，避免阻塞到函数总时限。',
  '外部服务报错：检查部署进程的环境变量、网络和对方接口；不要把凭据写入通知正文或日志。',
]

function table(rows: NotificationField[]): string {
  return ['| 字段 | 类型 | 含义 |', '| --- | --- | --- |', ...rows.map(({ name, type, desc }) => `| \`${name}\` | \`${type}\` | ${desc} |`)].join('\n')
}

function bullets(items: string[]): string { return items.map((item) => `- ${item}`).join('\n') }

export function buildNotificationMarkdown(): string {
  return `# 账户执行通知函数

用同步或异步 notify(context) 接收执行结束时的脱敏快照，自行发送账户执行通知。

## 函数契约

${bullets(CONTRACT_RULES)}

## 可复制示例

\`\`\`python
${NOTIFY_CODE}
\`\`\`

示例通过部署进程的 AXILE_NOTIFY_WEBHOOK 环境变量读取目标地址。试跑时会跳过发送。

### 异步示例

\`\`\`python
${ASYNC_NOTIFY_CODE}
\`\`\`

入口会等待异步请求完成，再报告运行成功；总时限仍为 15 秒。

## 账户执行 context

${table(NOTIFICATION_FIELDS)}

### 常用嵌套字段

${table(EXECUTION_FIELDS)}

context 是执行结束时的 JSON 可序列化快照。资产不可用时相关值可能为 None；列表也可能为空。

## 试跑与真实执行

${bullets(TEST_RULES)}

## 独立补发

每次实际执行均调用同一个 hook。\`event_type=execution.finished\` 保留执行上下文，\`execution.supplements[]\` 提供 group_id、base_scheduled_at、scheduled_at、index（首轮为 0）、configured_count、effective_count、is_last、group_status、end_reason。合并执行保留所有触发关联，只通知一次。

\`event_type=supplement.cancelled\` 的 \`execution=null\`。\`supplement\` 包含 reason、cancel_requested_at、取消步骤及 execution_id。相关执行收尾后才通知；\`last_execution\` 为可空快照，\`last_execution_at\` 标明快照时间。

待投递事件持久化，按账户顺序派发。静默返回仍算函数成功；失败和调用结果未知均不自动重试，重启只恢复尚未开始的调用。

仅末次和取消通知，可把以下过滤条件放在自定义发送逻辑之前（send 由用户实现）：

\`\`\`python
def notify(context):
    if context.get("event_type") == "supplement.cancelled":
        send(context)
        return
    groups = (context.get("execution") or {}).get("supplements", [])
    if any(item["group_status"] == "cancelling" for item in groups):
        return
    if groups and not any(item["is_last"] for item in groups):
        return
    send(context)
\`\`\`

## 常见错误

${bullets(FAILURE_RULES)}
`
}
