import { expect, test } from '@playwright/test'

test.beforeEach(async ({ request }) => { await request.post('/api/v1/test/orders/reset') })

for (const tab of ['最近成交', '最近委托', '撤单 / 拒绝', '操作异常']) {
  test(`${tab} uses a quiet read warning and preserves reconciliation failures`, async ({ page }) => {
    let failure: 'http' | 'partial' | null = null
    await page.route('**/api/v1/account/1/orders/activity?**', async route => {
      const mode = failure
      if (mode === 'http') return route.fulfill({ status: 502, json: { detail: '柜台记录读取超时' } })
      const response = await route.fetch()
      const data = await response.json()
      if (mode === 'partial') data.errors = ['渠道部分记录暂不可用']
      await route.fulfill({ json: data })
    })
    await page.goto('/e2e/orders.html')
    await page.getByRole('button', { name: '持仓、委托与成交 · 在途 1', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '持仓、委托与成交', exact: true })
    await dialog.getByRole('button', { name: tab, exact: true }).click()
    await expect(dialog.getByRole('button', { name: '核对记录', exact: true })).toBeEnabled()
    await expect(dialog).not.toContainText('记录观测：尚未读取')
    const rows = dialog.locator('[data-swap-current] [data-activity-row]')
    const rowCount = await rows.count()
    await page.clock.install()
    await page.clock.pauseAt(new Date())
    const read = () => page.evaluate(async () => {
      const path = '/src/lib/readRecovery.ts'
      const { notifyReadRecovery } = await import(path)
      await notifyReadRecovery()
    })
    const delayed = dialog.getByRole('img', { name: '记录更新暂不可用', exact: true })
    failure = 'http'
    await read()
    await page.clock.runFor(4999)
    await expect(delayed).toHaveCount(0)
    await page.clock.runFor(1)
    await expect(delayed).toBeVisible()
    await expect(rows).toHaveCount(rowCount)
    await expect(dialog).not.toContainText('记录读取失败')
    await delayed.focus()
    await page.clock.runFor(200)
    await expect(page.getByRole('tooltip')).toContainText('柜台记录读取超时')
    failure = null
    await read()
    await expect(delayed).toHaveCount(0)

    // 显式核对失败立即提示，读取缓存成功不能冒充渠道核对成功。
    failure = 'http'
    await dialog.getByRole('button', { name: '核对记录', exact: true }).click()
    const manual = dialog.getByRole('img', { name: '本次核对未完成', exact: true })
    await expect(manual).toBeVisible()
    failure = null
    await read()
    await expect(manual).toBeVisible()
    await dialog.getByRole('button', { name: '核对记录', exact: true }).click()
    await expect(dialog.getByRole('button', { name: '核对记录', exact: true })).toBeEnabled()
    await expect(manual).toHaveCount(0)

    // HTTP 成功但渠道报告读取不完整，也用同一个小点保留原因。
    failure = 'partial'
    await read()
    await page.clock.runFor(5000)
    await expect(delayed).toBeVisible()
    await delayed.focus()
    await page.clock.runFor(200)
    await expect(page.getByRole('tooltip')).toContainText('渠道部分记录暂不可用')
    await expect(rows).toHaveCount(rowCount)
    failure = null
    await read()
    await expect(delayed).toHaveCount(0)
  })
}
