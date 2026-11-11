import { expect, test } from 'bun:test'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import { AlgorithmWorkbench } from '@/features/account/AlgorithmWorkbench'
import {
  algorithmScheduleDraft, algorithmScheduleErrors, algorithmScheduleValue, algorithmStartChange, algorithmTimeLabel,
} from '@/features/account/algorithmSchedule'
import type { AlgorithmTimeRule } from '@/types/api'

const rule = (start = '09:30', end = '10:00'): AlgorithmTimeRule => ({ start, end, algorithm: { method: 'TWAP', params: { total_duration: 120, slices: 4 } } })

test('未设置的结束时间补开始后一分，自动值跟随调整，手动值和已保存值保留', () => {
  let draft = { ...rule('', ''), key: 'new-rule' }
  draft = { ...draft, ...algorithmStartChange(draft, '09:59') }
  expect(draft).toMatchObject({ start: '09:59', end: '10:00', autoEnd: true })
  draft = { ...draft, ...algorithmStartChange(draft, '23:59') }
  expect(draft).toMatchObject({ start: '23:59', end: '00:00', autoEnd: true })
  expect(algorithmScheduleErrors([draft])).toEqual([null])
  expect(algorithmStartChange({ ...draft, autoEnd: false }, '22:00')).toEqual({ start: '22:00' })
  expect(algorithmStartChange({ ...rule(), key: 'existing' }, '09:31')).toEqual({ start: '09:31' })
  const value = algorithmScheduleValue([draft])
  expect(Object.keys(value[0]).sort()).toEqual(['algorithm', 'end', 'start'])
  const saved = algorithmScheduleDraft(value, [draft])[0]
  expect(saved.key).toBe(draft.key)
  expect(algorithmStartChange(saved, '23:58')).toEqual({ start: '23:58' })
})

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
  expect(algorithmScheduleDraft(algorithmScheduleValue(draft), draft)[0].key).toBe(draft[0].key)
})

test('工作区提供默认、时段和清仓入口，未选中的表单保留并禁止聚焦', () => {
  const html = renderToStaticMarkup(createElement(AlgorithmWorkbench, {
    channel: 'ctp', trade: rule().algorithm, empty: null, schedule: algorithmScheduleDraft([rule()]),
    errors: {}, footer: createElement('div', null, '左栏公共设置'), saveBar: createElement('button', null, '保存修改'),
    onTradeChange() {}, onEmptyChange() {}, onScheduleChange() {}, onValidationError() {},
  }))
  expect(html).toContain('编辑默认算法')
  expect(html).toContain('编辑09:30 至 10:00')
  expect(html).toContain('编辑清仓算法')
  expect(html).toContain('当前算法编辑区')
  expect(html).toContain('时段参数 1')
  expect(html).toContain('清仓算法参数')
  expect(html).toContain('inert=""')
  const right = html.slice(html.indexOf('<section aria-label="当前算法编辑区"'))
  expect(right).toContain('保存修改')
  expect(right).not.toContain('左栏公共设置')
  expect(html.match(/保存修改/g)).toHaveLength(1)
})

test('当日与跨日保留摘要和编辑区的次日节点，收起时对辅助技术隐藏', () => {
  const render = (end: string) => renderToStaticMarkup(createElement(AlgorithmWorkbench, {
    channel: 'ctp', trade: rule().algorithm, empty: null, schedule: algorithmScheduleDraft([rule('09:30', end)]),
    errors: {}, footer: null, saveBar: null, onTradeChange() {}, onEmptyChange() {}, onScheduleChange() {}, onValidationError() {},
  }))
  for (const [end, nextDay] of [['09:00', true], ['10:00', false], ['09:30', false], ['', false]] as const) {
    const html = render(end)
    const count = end ? 2 : 1
    expect(html.match(/>次日<\/span>/g)?.length).toBe(count)
    const markers = html.match(/<span aria-hidden="(?:true|false)"[^>]*transition-\[grid-template-columns\][^>]*>/g) ?? []
    expect(markers).toHaveLength(count)
    for (const marker of markers) {
      expect(marker).toContain(`aria-hidden="${!nextDay}"`)
      expect(marker.includes('inert=""')).toBe(!nextDay)
      expect(marker).toContain(nextDay ? 'grid-cols-[1fr]' : 'grid-cols-[0fr]')
      expect(marker).toContain('motion-reduce:transition-none')
    }
    if (end) expect(html).toContain(`编辑09:30 至 ${nextDay ? '次日 ' : ''}${end}`)
  }
})
