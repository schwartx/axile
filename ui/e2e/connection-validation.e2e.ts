import { expect, test } from '@playwright/test'

const table = {
  kind: 'table', title: '订单查询记录', summary: '60 条订单', row_count: 60,
  columns: [
    { key: 'symbol', label: '合约', type: 'text' },
    { key: 'exchange', label: '交易所', type: 'text', filterable: true },
    { key: 'quantity', label: '手数', type: 'number' },
    { key: 'id', label: '订单编号', type: 'text' },
  ],
  rows: Array.from({ length: 60 }, (_, i) => [`ag${String(i + 1).padStart(4, '0')}`, i % 2 ? 'SHFE' : 'DCE', 60 - i, `000${i}`]),
}
const { rows: _rows, ...tableSummary } = table
const steps = [
  { id: 'trader_connection', label: '交易前置连接' }, { id: 'authenticate', label: '授权码认证' },
  { id: 'trader_login', label: '交易账户登录', details: { title: '交易账户登录信息', rows: [{ key: '用户代码', value: 'full-account' }, { key: '前置编号', value: 0 }] } },
  { id: 'settlement', label: '结算确认' },
  { id: 'instruments', label: '合约查询（螺纹钢）', details: { ...tableSummary, title: '合约查询记录', summary: '12 个螺纹钢合约', row_count: 12 } },
  { id: 'market_login', label: '行情登录' }, { id: 'query_orders', label: '订单查询', details: tableSummary },
  { id: 'query_trades', label: '成交查询' },
  { id: 'query_positions', label: '持仓查询', details: { ...tableSummary, title: '持仓查询记录', summary: '0 条持仓记录', row_count: 0 } },
  { id: 'query_account', label: '资金查询', details: { title: '资金查询记录', rows: [{ key: '资金账号', value: 'full-account' }, { key: '权益', value: 0 }, { key: '可用资金', value: null }] } },
  { id: 'ready', label: '会话就绪', status: 'failed', error: { reason_code: 'TEST_FAILURE', message: '会话整理失败' } },
].map((step) => ({ status: 'passed', ...step }))
const job = { validation_id: 'test-validation', run_id: 'test-validation', purpose: 'validation', channel: 'ctp', status: 'failed', steps, created_at: '2026-10-13T10:00:00+08:00', finished_at: '2026-10-13T10:00:01+08:00', error: null }

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => { throw error })
  await page.route('**/api/v1/channel-validations', (route) => route.fulfill({ json: job }))
  await page.goto('/e2e/connection-validation.html')
  await page.getByRole('button', { name: '验证连接', exact: true }).click()
})

test('key-value details and lazy records support filtering sorting pagination and cache', async ({ page }) => {
  let reads = 0
  await page.route('**/steps/query_orders/table', (route) => { reads++; return route.fulfill({ json: table }) })
  const orders = page.locator('[data-connection-step="query_orders"]')
  await expect(orders).toContainText('60 条')
  await expect(page.locator('[data-connection-step="trader_connection"]').getByRole('button')).toHaveCount(0)
  expect(reads).toBe(0)
  await page.screenshot({ path: '/tmp/axon-validation-compact.png', fullPage: true })
  const login = page.locator('[data-connection-step="trader_login"]').getByRole('button', { name: '查看详情' })
  await login.click()
  const loginDialog = page.getByRole('dialog', { name: '交易账户登录信息' })
  await expect(loginDialog).toContainText('full-account')
  await expect(loginDialog.getByText('0', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(login).toBeFocused()
  await orders.getByRole('button', { name: '查看详情' }).click()
  const dialog = page.getByRole('dialog', { name: '订单查询记录' })
  await expect(dialog.getByRole('row')).toHaveCount(26)
  await dialog.getByRole('button', { name: '下一页' }).click()
  await expect(dialog.getByText('2 / 3', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: '手数' }).click()
  await expect(dialog.getByRole('row').nth(1)).toContainText('ag0060')
  await dialog.getByRole('combobox', { name: '筛选交易所' }).selectOption('SHFE')
  await expect(dialog.getByText('共 60 条，筛选后 30 条')).toBeVisible()
  await dialog.getByRole('searchbox').fill('ag0060')
  await expect(dialog.getByRole('row')).toHaveCount(2)
  await expect(dialog).toContainText('00059')
  await dialog.getByRole('searchbox').fill('no-match')
  await expect(dialog).toContainText('没有匹配的记录')
  await page.keyboard.press('Escape')
  await orders.getByRole('button', { name: '查看详情' }).click()
  await expect(dialog.getByRole('row')).toHaveCount(26)
  expect(reads).toBe(1)
  await page.screenshot({ path: '/tmp/axon-validation-table.png', fullPage: true })
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '修改配置' }).click()
  await expect(orders.getByRole('button', { name: '查看详情' })).toHaveCount(0)
})

test('expired data requires validation again before empty snapshots are shown', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  let tries = 0
  await page.route('**/steps/query_positions/table', (route) => {
    tries++
    return tries === 1 ? route.fulfill({ status: 404, json: { detail: '验证任务不存在或已过期，请重新验证' } })
      : route.fulfill({ json: { ...table, title: '持仓查询记录', rows: [], row_count: 0 } })
  })
  await page.locator('[data-connection-step="query_positions"]').getByRole('button', { name: '查看详情' }).click()
  const dialog = page.getByRole('dialog', { name: '持仓查询记录' })
  await expect(dialog).toContainText('过程记录已过期或被新连接替换')
  await expect(dialog.getByRole('button', { name: '重试：查询记录读取失败' })).toHaveCount(0)
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '重新验证', exact: true }).click()
  await page.locator('[data-connection-step="query_positions"]').getByRole('button', { name: '查看详情' }).click()
  await expect(dialog).toContainText('本次查询无记录')
  await expect(dialog.getByRole('button', { name: '下一页' })).toBeDisabled()
  await page.screenshot({ path: '/tmp/axon-validation-mobile.png', fullPage: true })
  await page.keyboard.press('Escape')
  await page.locator('[data-connection-step="query_account"]').getByRole('button', { name: '查看详情' }).click()
  await expect(page.getByRole('dialog', { name: '资金查询记录' })).toContainText('未返回')
})


test('narrow layout aligns the trigger with the section heading and keeps it above fields', async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 800 })
  await page.goto('/e2e/connection-validation.html?layout')
  const trigger = page.getByRole('button', { name: '连接验证', exact: true })
  const heading = await page.getByText('CTP 连接', { exact: true }).boundingBox()
  const button = await trigger.boundingBox()
  const field = await page.getByRole('textbox', { name: '投资者号' }).boundingBox()
  expect(heading).not.toBeNull()
  expect(button).not.toBeNull()
  expect(field).not.toBeNull()
  expect(Math.abs(heading!.y + heading!.height / 2 - button!.y - button!.height / 2)).toBeLessThan(1)
  expect(button!.y + button!.height).toBeLessThan(field!.y)
  await trigger.click()
  const dialog = page.getByRole('dialog', { name: '账户连接验证面板' })
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '验证连接', exact: true }).click()
  await expect(dialog).toContainText('账户连接验证未通过')
  await page.keyboard.press('Escape')
  await expect(trigger).toBeFocused()
  await trigger.click()
  await expect(dialog).toContainText('账户连接验证未通过')
  await page.keyboard.press('Escape')
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(trigger).toBeHidden()
  await expect(page.getByRole('region', { name: '账户连接验证', exact: true })).toBeVisible()
})

test('server outage still allows an explicit validation attempt without a duplicate connection warning', async ({ page }) => {
  await page.getByRole('button', { name: '模拟服务失联' }).click()
  await expect(page.getByRole('button', { name: '重新验证', exact: true })).toBeEnabled()
  await expect(page.getByText('无法连接 axile 服务，连接验证暂不可用。', { exact: false })).toHaveCount(0)
})

test('table query keeps HTTP code request id and full raw error inside its current dialog', async ({ page }) => {
  const raw = 'synthetic table error '.repeat(100) + '\npassword=synthetic token=synthetic api_key=synthetic\n尾部根因'
  await page.route('**/steps/query_orders/table', route => route.fulfill({ status: 500, json: { detail: { code: 'TABLE_FAILED', request_id: 'table-97', message: raw, traceback: 'Traceback\n  table.py:123\n' + raw } } }))
  await page.locator('[data-connection-step="query_orders"]').getByRole('button', { name: '查看详情' }).click()
  const dialog = page.getByRole('dialog', { name: '订单查询记录' })
  await dialog.getByRole('button', { name: '查看错误详情' }).click()
  await expect(dialog).toContainText('TABLE_FAILED')
  await expect(dialog).toContainText('table-97')
  await expect(dialog).toContainText('尾部根因')
  await expect(page.getByRole('dialog')).toHaveCount(1)
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { value: { writeText: async (text: string) => { (window as unknown as { copied: string }).copied = text } } }))
  await dialog.getByRole('button', { name: '复制错误信息' }).click()
  const content = await page.evaluate(() => (window as unknown as { copied: string }).copied)
  expect(content).toContain(raw)
  expect(content).toContain('Traceback\n  table.py:123\n')
})

test('validation request network failure keeps its job and rereads it without creating another', async ({ page }) => {
  let creates = 0, reads = 0
  const running = { ...job, status: 'running', finished_at: null }
  await page.route('**/api/v1/channel-validations', route => { creates++; return route.fulfill({ json: running }) })
  await page.route('**/api/v1/channel-validations/test-validation', route => {
    reads++
    return reads === 1 ? route.fulfill({ status: 500, json: { detail: { message: '网络读取暂时失败', code: 'VALIDATION_READ_FAILED', request_id: 'read-97' } } }) : route.fulfill({ json: job })
  })
  await page.getByRole('button', { name: '重新验证', exact: true }).click()
  const notice = page.getByRole('status').filter({ hasText: '连接验证请求失败' })
  await expect(notice).toContainText('网络读取暂时失败')
  await notice.getByRole('button', { name: '查看错误详情' }).click()
  await expect(page.getByRole('dialog')).toContainText('read-97')
  await expect(page.getByRole('dialog')).toContainText('VALIDATION_READ_FAILED')
  await page.keyboard.press('Escape')
  await notice.getByRole('button', { name: '重试：连接验证请求失败' }).click()
  await expect.poll(() => reads).toBe(2)
  expect(creates).toBe(1)
})
