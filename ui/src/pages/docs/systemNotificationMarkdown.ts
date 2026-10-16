export const SYSTEM_NOTIFICATION_FIELDS = [
  ['event_id', 'str', '系统事件标识。'],
  ['event_type', 'str', 'execution_error（执行异常）、execution_timeout（总超时）或 account_control.warning（额度预警）。'],
  ['execution_id', 'str | None', '关联执行 ID；试跑时为 None。'],
  ['occurred_at', 'str', '服务本地时间的 ISO 字符串。'],
  ['account', 'dict | None', '关联账户的 id、name；没有账户时为 None。'],
  ['error', 'dict | None', '异常事件的错误信息；额度预警为 None。'],
  ['error.type', 'str', '异常类型。'],
  ['error.message', 'str', '异常消息。'],
  ['error.traceback', 'str', '来自异常对象的堆栈。'],
  ['quota', 'dict（仅额度预警）', '触发时的账户级每日额度快照；其他事件不提供此字段。'],
  ['quota.operation', 'str', 'place_order（报单）或 cancel_order（撤单），分别计数；不叠加原生撤单。'],
  ['quota.used / limit / remaining', 'int', '触发时的用量、限额和剩余次数。'],
  ['quota.warning_percent / threshold_count', 'int', '预警比例（1–99）及向上取整后的触发次数。'],
  ['quota.control_date / timezone', 'str', '统计日期与流控时区；同一账户、操作、统计日只入队一次。'],
  ['quota.source', 'str', '固定为 axile_local_allowed；仅表示本地流控放行次数，不代表柜台受理或成交。'],
  ['is_test', 'bool', '草稿试跑为 True；真实告警为 False。'],
]
export const SYSTEM_NOTIFICATION_RULES = [
  '保留默认与自定义两种模式。默认模式使用内置飞书卡片；自定义模式运行保存的 notify(context)。切换并保存不会删除另一种模式的配置。',
  'notify 必须只接收一个 context 参数，支持同步或异步函数；自定义模式不允许保存空函数。',
  '系统飞书 Key 通过环境变量 AXILE_SYSTEM_FEISHU_KEY 提供，不在 context 中返回。账户 Key 不注入系统通知函数。',
  '函数在独立进程中运行，总时限为 15 秒；网络请求应设置更短的超时。失败只记录通知结果，不改变交易结论，不自动补发默认通知。',
  '报单、撤单的账户级每日额度可在执行流控页设置预警比例，保存后下次执行生效。达到预警线只提醒，排队和阻断仍由原额度规则决定；无限额或额度为 0 时预警暂停。',
  '管理页面在达到预警线和额度用尽时分别自动显示浮窗，首次进入时补弹未提醒的状态；同一浏览器内按账户、操作、统计时区和日期去重。关闭浮窗不清除页面额度状态，额度用尽的页面提醒不依赖预警比例开关。',
  '已有自定义函数需要增加 account_control.warning 分支，不能假设每种事件都提供 error 字典。用户保存的源码不会自动改写；可先用额度预警样例试跑。',
  '试跑使用当前草稿与当前凭据选择，可选执行异常、超时或额度预警样例。试跑可能真实发送消息，但不保存配置、不覆盖最近真实告警结果。',
  '预警复用持久化通知队列；失败或调用结果未知的消息不会自动重发。最近系统告警结果可用于检查发送情况。',
  'Webhook 输入留空保留服务端已保存凭据；清除 Webhook 后保存才会删除。默认模式无 Webhook 时不发送。',
]
export const SYSTEM_NOTIFICATION_EXAMPLE = `import os
from axile.common.feishu import push_feishu_card
from axile.common.notification_context import SystemNotificationContext
from axile.common.quota_warning_card import build_quota_warning_card


def notify(context: SystemNotificationContext) -> None:
    key = os.environ.get("AXILE_SYSTEM_FEISHU_KEY", "")
    if not key:
        raise ValueError("请配置系统飞书 Webhook")
    if context["event_type"] == "account_control.warning":
        push_feishu_card(build_quota_warning_card(context), key, timeout=8)
        return
    error = context["error"]
    assert error is not None
    title = "系统告警试跑" if context["is_test"] else "系统执行告警"
    push_feishu_card({
        "header": {"title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "markdown", "content": error["message"]}],
    }, key, timeout=8)
`
export function buildSystemNotificationMarkdown() {
  return `# 系统告警函数\n\n## 函数契约\n\n${SYSTEM_NOTIFICATION_RULES.map((rule) => `- ${rule}`).join('\n')}\n\n## 可复制示例\n\n\`\`\`python\n${SYSTEM_NOTIFICATION_EXAMPLE}\`\`\`\n\n## 系统告警 context\n\n| 字段 | 类型 | 含义 |\n| --- | --- | --- |\n${SYSTEM_NOTIFICATION_FIELDS.map((row) => `| ${row.map((cell) => cell.replaceAll('|', '\\|')).join(' | ')} |`).join('\n')}`
}
