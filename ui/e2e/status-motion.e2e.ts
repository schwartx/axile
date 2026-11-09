import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }) => { page.on('pageerror', error => { throw error }) })

for (const reduced of [false, true]) {
  test(`live values and status text update without changing saving behavior (reduced=${reduced})`, async ({ page }, testInfo) => {
    await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' })
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.goto('/e2e/status-motion.html')
    const summary = page.getByRole('region', { name: '流控摘要', exact: true })
    const sync = page.getByRole('region', { name: '运行态同步', exact: true })
    const clock = page.getByRole('region', { name: '检查结果', exact: true })
    const flows = page.locator('number-flow-react')
    await expect(flows).toHaveCount(6)
    await flows.evaluateAll(elements => elements.forEach(element => {
      element.addEventListener('animationsstart', () => { (element as HTMLElement).dataset.started = 'true' })
    }))
    await page.getByRole('button', { name: '更新合成状态', exact: true }).click()
    await expect(sync.locator('.ink-rewrite-slot > span:last-child')).toHaveText('配置已保存，运行态已同步')
    await expect(clock.locator('.ink-rewrite-slot > span:last-child').first()).toHaveText('本机慢')
    await expect(clock.locator('number-flow-react .digit__num:not([inert])')).toHaveText(['1', '2', '3', '4'])
    await expect.poll(() => flows.evaluateAll(elements => elements.some(element => (element as HTMLElement).dataset.started === 'true'))).toBe(!reduced)
    await expect(summary.getByRole('button', { name: /^等待请求/ })).toBeVisible()
    await summary.getByRole('button', { name: /^等待请求/ }).click()
    const waiting = summary.getByRole('region', { name: '等待请求', exact: true })
    await expect(waiting).toContainText('已等待 1 分 30.1 秒')
    await expect(waiting).toContainText('预计再等 1 时 0.1 秒')
    await expect(waiting.locator('number-flow-react')).toHaveCount(0)
    const save = page.getByRole('button', { name: '保存', exact: true })
    const before = (await save.boundingBox())!
    await save.click()
    const saving = page.getByRole('button', { name: '保存中…', exact: true })
    await expect(saving).toBeDisabled()
    expect(await saving.boundingBox()).toEqual(before)
    await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), !reduced)
    await page.screenshot({ path: testInfo.outputPath(`status-motion-${reduced}.png`), animations: 'disabled' })
    await page.getByRole('button', { name: '完成保存', exact: true }).click()
    await expect(saving).toHaveCount(0)
    await expect(page.getByRole('button', { name: '保存', exact: true })).toHaveCount(0)
  })
}

test('stale and unknown control snapshots retain meaning and stop number animations', async ({ page }) => {
  await page.goto('/e2e/status-motion.html')
  const summary = page.getByRole('region', { name: '流控摘要', exact: true })
  await page.getByRole('button', { name: '保留陈旧快照', exact: true }).click()
  await expect(summary.locator('number-flow-react')).toHaveCount(0)
  await expect(summary).toContainText('今日已用 20/100 · 剩余 80')
  await page.getByRole('button', { name: '用量未知', exact: true }).click()
  await expect(summary).toContainText('今日用量未知')
  await expect(summary.getByRole('button', { name: '等待请求(未知)', exact: true })).toBeVisible()
  await expect(summary.locator('number-flow-react')).toHaveCount(0)
})

test('freshness ticks do not rewrite connection status and connection errors stay actionable', async ({ page }) => {
  await page.clock.install()
  await page.goto('/e2e/status-motion.html')
  const service = page.getByRole('region', { name: '服务连接', exact: true })
  const status = service.locator('.ink-rewrite-slot > span:last-child')
  await expect(status).toContainText('服务正常')
  await page.getByRole('button', { name: '更新新鲜度', exact: true }).click()
  await page.clock.runFor(1000)
  await expect(service).toContainText('数据 11 秒前')
  await expect(status).not.toHaveClass(/ink-rewrite-in/)
  await page.getByRole('button', { name: '模拟连接故障', exact: true }).click()
  await expect(status).toContainText('与服务器失联')
  const details = service.getByRole('button', { name: '查看服务连接错误详情', exact: true })
  await details.click()
  await expect(page.getByRole('dialog', { name: '与服务器失联', exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '恢复连接', exact: true }).click()
  await expect(details).toHaveCount(0)
  await expect(status).toContainText('服务正常')
})

test('frozen execution timeout uses duration text and keeps the original seconds in evidence', async ({ page }) => {
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/artifacts')) return route.fulfill({ json: { count: 1, data: [{
      id: 1, execution_id: 'A', artifact_type: 'standard_input', content: { input: { execution_timeout: 7215 } }, created_at: '',
    }] } })
    if (path.endsWith('/executions/A')) return route.fulfill({ json: {
      execution_id: 'A', account_id: 7, execution_kind: 'rebalance', status: 'SUCCEEDED',
      created_at: '2026-11-09T10:00:00', started_at: null, finished_at: '2026-11-09T10:01:00', error_message: null,
    } })
    return route.fulfill({ json: { data: [], count: 0 } })
  })
  await page.goto('/e2e/evidence.html')
  await expect(page.getByText('冻结输入 · 执行超时 2 时 15 秒', { exact: true })).toBeVisible()
  expect(await page.evaluate(async () => (await fetch('/api/v1/account/executions/A/artifacts')).json()))
    .toMatchObject({ data: [{ content: { input: { execution_timeout: 7215 } } }] })
})
