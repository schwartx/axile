import { CRON_FIELDS } from '@/features/setup/cronSettingModel'

export function CronSettingHelp() {
  return <div className="mb-3 shrink-0 space-y-3 rounded-lg bg-fill p-3 text-[13px] leading-relaxed text-ink-2">
    <div>
      <div className="mb-2 font-semibold">Cron 表达式格式：分 时 日 月 周</div>
      {CRON_FIELDS.map((field) => <p key={field.id} className="mb-1">{field.label}：{field.min}–{field.max}{field.id === 'week' ? '（0 表示周一，6 表示周日）' : ''}</p>)}
      <p>时间均为北京时间；日期与星期限制同时生效。</p>
    </div>
    <div>
      <div className="mb-2 font-semibold">特殊字符说明</div>
      <p>*：表示所有值</p><p>,：分隔多个值</p><p>-：表示一个范围</p><p>/：表示间隔频率</p>
    </div>
    <div>
      <div className="mb-2 font-semibold">示例</div>
      <p><code>0 9 * * 0-4</code>：周一至周五 9 点执行</p>
      <p><code>*/15 * * * *</code>：每 15 分钟执行一次</p>
      <p><code>0 0 1,15 * *</code>：每月 1 日和 15 日 0 点执行</p>
      <p><code>0-59 0-1 * * *</code>：每天 0 点至 1 点 59 分，每分钟执行</p>
      <p><code>0-10 * * * *</code>：每小时 0 分至 10 分，每分钟执行</p>
      <p><code>1,2 * * * *</code>：每小时第 1 分钟和第 2 分钟执行</p>
    </div>
  </div>
}
