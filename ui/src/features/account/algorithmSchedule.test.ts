import { expect, test } from 'bun:test'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import { EditableAlgorithm } from '@/features/account/AlgorithmScheduleEditor'
import {
  algorithmParameterSummary, algorithmScheduleDraft, algorithmScheduleErrors, algorithmScheduleValue, algorithmTimeLabel,
} from '@/features/account/algorithmSchedule'
import type { AlgorithmTimeRule } from '@/types/api'

const rule = (start = '09:30', end = '10:00'): AlgorithmTimeRule => ({ start, end, algorithm: { method: 'TWAP', params: { total_duration: 120, slices: 4 } } })

test('时段校验允许相邻和午夜，指出跨日冲突', () => {
  expect(algorithmScheduleErrors([rule(), rule('10:00', '11:00')])).toEqual([null, null])
  expect(algorithmScheduleErrors([rule('21:00', '00:00'), rule('00:00', '02:30')])).toEqual([null, null])
  expect(algorithmScheduleErrors([rule('21:00', '02:30'), rule('01:00', '03:00')]).every(Boolean)).toBe(true)
  expect(algorithmScheduleErrors([rule('', ''), rule('09:30', '09:30')]).every(Boolean)).toBe(true)
  expect(algorithmTimeLabel('21:00', '02:30')).toBe('21:00 至 次日 02:30')
})

test('草稿独立于已保存配置，提交只包含 API 字段并按时间排序', () => {
  const saved = [rule('21:00', '02:30'), rule()]
  const draft = algorithmScheduleDraft(saved)
  draft[0].algorithm.params.slices = 6
  expect(saved[1].algorithm.params.slices).toBe(4)
  expect(algorithmScheduleValue(draft)).toEqual([{ ...saved[1], algorithm: { ...saved[1].algorithm, params: { total_duration: 120, slices: 6 } } }, saved[0]])
  expect(algorithmParameterSummary(draft[0].algorithm)).toBe('持续 120 秒 · 分 6 次')
})

test('算法折叠时仍显示摘要，隐藏参数不会进入键盘焦点', () => {
  const html = renderToStaticMarkup(createElement(EditableAlgorithm, {
    label: '平时使用', channel: 'ctp', value: rule().algorithm, onChange() {},
  }))
  expect(html).toContain('持续 120 秒')
  expect(html).toContain('aria-expanded="false"')
  expect(html).toContain('inert=""')
})
