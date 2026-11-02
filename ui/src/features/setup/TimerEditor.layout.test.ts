import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { TimerEditor } from '@/features/setup/TimerEditor'
import { defaultTimerEditorState } from '@/features/setup/cron'
import { WizardHeading } from '@/features/setup/WizardNav'

function renderEditor(
  layout: 'page' | 'step',
  leading?: ReturnType<typeof createElement>,
) {
  return renderToStaticMarkup(
    createElement(TimerEditor, {
      tradeChannel: 'ctp',
      scheduleKind: 'cn_futures',
      value: defaultTimerEditorState('cn_futures'),
      layout,
      leading,
      onChange: () => {},
    }),
  )
}

test('page 布局把 leading 放进左列，预览仍是同一网格的右列', () => {
  const html = renderEditor('page', createElement('h1', null, '定时任务 · TQ'))
  expect(html).toContain('定时任务 · TQ')
  expect(html).toContain('排程预览')
  expect(html.indexOf('定时任务 · TQ')).toBeLessThan(html.indexOf('排程预览'))
  expect(html.indexOf('定时任务 · TQ')).toBeLessThan(html.indexOf('自动调仓'))
  expect(html).toContain('min-[1120px]:grid-cols-[minmax(0,1fr)_clamp(360px,33%,520px)]')
  expect(html).toContain('<aside')
})

test('向导标题作为 leading 与预览顶对齐，走同一套右栏卡片', () => {
  const html = renderEditor(
    'page',
    createElement(WizardHeading, {
      kicker: '账户设置 · 5 / 6',
      title: '什么时候自动执行？',
      lead: '开启后 axile 按下面的节奏自动调仓。时间均为北京时间。',
    }),
  )
  expect(html.indexOf('什么时候自动执行？')).toBeLessThan(html.indexOf('排程预览'))
  expect(html.indexOf('什么时候自动执行？')).toBeLessThan(html.indexOf('自动调仓'))
  expect(html).toContain('min-[1120px]:grid-cols-[minmax(0,1fr)_clamp(360px,33%,520px)]')
  expect(html).toContain('<aside')
  expect(html).not.toContain('border-y border-line')
})

test('step 布局预览沉底，没有右栏网格', () => {
  const html = renderEditor('step')
  expect(html).not.toContain('min-[1120px]:grid-cols-[minmax(0,1fr)_clamp(360px,33%,520px)]')
  expect(html).toContain('border-y border-line')
  expect(html).not.toContain('<aside')
  expect(html.indexOf('自动调仓')).toBeLessThan(html.indexOf('排程预览'))
})

test('向导定时步把标题交给 page 布局的 leading', async () => {
  const src = await Bun.file(new URL('../../pages/setup/AccountSteps.tsx', import.meta.url)).text()
  const timer = src.slice(src.indexOf('export function AcctTimer'), src.indexOf('export function AcctConfirm'))
  expect(timer).toContain('layout="page"')
  expect(timer).toContain('leading={heading}')
  expect(timer).toContain('WizardHeading')
  expect(timer).not.toContain('<WizardPage')
})
