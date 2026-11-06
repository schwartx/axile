import { expect, test } from 'bun:test'
import { CRON_FIELDS, cronFieldError, cronRuleError, formatCronField, parseCronRule, updateCronRuleField, type CronFieldDraft } from '@/features/setup/cronSettingModel'

function change(rule: ReturnType<typeof parseCronRule>, index: number, patch: Partial<CronFieldDraft>) {
  return updateCronRuleField(rule, index, { ...rule.fields![index]!, ...patch, original: undefined })
}

test('指定值清空后保持非法草稿，不扩大成任意值', () => {
  for (let index = 0; index < CRON_FIELDS.length; index++) {
    const rule = parseCronRule('0 9 1 1 0')
    const cleared = change(rule, index, { values: [] })
    expect(cronRuleError(cleared)).toBe('请至少选择一项。')
    expect(formatCronField(CRON_FIELDS[index]!, cleared.fields![index]!)).toBe('')
  }
})

test('间隔、范围与指定值分别保留草稿，其他字段原文不变', () => {
  let rule = parseCronRule('1/4 9 last jan-mar mon-fri')
  rule = change(rule, 0, { mode: 'range', start: '10', end: '20' })
  expect(rule.raw).toBe('10-20 9 last jan-mar mon-fri')
  rule = change(rule, 0, { mode: 'specific', values: [5, 15] })
  expect(rule.raw).toBe('5,15 9 last jan-mar mon-fri')
  rule = change(rule, 0, { mode: 'interval' })
  expect(rule.raw).toBe('1/4 9 last jan-mar mon-fri')
  rule = change(rule, 0, { mode: 'range' })
  expect(rule.raw).toBe('10-20 9 last jan-mar mon-fri')
  rule = change(rule, 0, { mode: 'every' })
  expect(cronRuleError(rule)).toBeNull()
  rule = change(rule, 0, { mode: 'specific' })
  expect(rule.raw).toBe('5,15 9 last jan-mar mon-fri')
})

test('非法数字保留供修正，校验拒绝范围倒序和超出跨度的间隔', () => {
  const rule = parseCronRule('0 9 * * *')
  const minute = CRON_FIELDS[0]!
  const draft = { ...rule.fields![0]!, original: undefined }
  for (const patch of [
    { mode: 'range' as const, start: '20', end: '10' },
    { mode: 'interval' as const, intervalStart: '58', step: '2' },
    { mode: 'interval' as const, intervalStart: '', step: '4' },
  ]) expect(cronFieldError(minute, { ...draft, ...patch })).not.toBeNull()
})
