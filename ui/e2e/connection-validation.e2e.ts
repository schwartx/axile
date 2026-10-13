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
  { id: 'settlement', label: '结算确认' }, { id: 'instruments', label: '合约目录查询' },
  { id: 'market_login', label: '行情登录' }, { id: 'query_orders', label: '订单查询', details: tableSummary },
  { id: 'query_trades', label: '成交查询' },
  { id: 'query_positions', label: '持仓查询', details: { ...tableSummary, title: '持仓查询记录', summary: '0 条持仓记录', row_count: 0 } },
  { id: 'query_account', label: '资金查询', details: { title: '资金查询记录', rows: [{ key: '资金账号', value: 'full-account' }, { key: '权益', value: 0 }, { key: '可用资金', value: null }] } },
  { id: 'ready', label: '会话就绪', status: 'failed', error: { reason_code: 'TEST_FAILURE', message: '会话整理失败' } },
].map((step) => ({ status: 'passed', ...step }))
const job = { validation_id: 'test-validation', channel: 'ctp', status: 'failed', steps, created_at: '2026-10-13T10:00:00+08:00', finished_at: '2026-10-13T10:00:01+08:00', error: null }

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => { throw error })
  await page.route('**/api/v1/channel-validations', (route) => route.fulfill({ json: job }))
  await page.goto('/e2e/connection-validation.html')
  await page.getByRole('button', { name: '验证连接', exact: true }).click()
})

test('key-value details and lazy records support filtering sorting pagination and cache', async ({ page }) => {
  let reads = 0
  await page.route('**/steps/query_orders/table', (route) => { reads++; return route.fulfill({ json: table }) })
  const orders = page.locator('[data-validation-step="query_orders"]')
  await expect(orders).toContainText('60 条订单')
  await expect(page.locator('[data-validation-step="trader_connection"]').getByRole('button')).toHaveCount(0)
  expect(reads).toBe(0)
  const login = page.locator('[data-validation-step="trader_login"]').getByRole('button', { name: '查看详情' })
  await login.click()
  const loginDialog = page.getByRole('dialog', { name: '交易账户登录信息' })
  await expect(loginDialog).toContainText('full-account')
  await expect(loginDialog.getByRole('cell', { name: '0', exact: true })).toBeVisible()
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

test('empty snapshots and expired data have distinct states with retry', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  let tries = 0
  await page.route('**/steps/query_positions/table', (route) => {
    tries++
    return tries === 1 ? route.fulfill({ status: 404, json: { detail: '验证任务不存在或已过期，请重新验证' } })
      : route.fulfill({ json: { ...table, title: '持仓查询记录', rows: [], row_count: 0 } })
  })
  await page.locator('[data-validation-step="query_positions"]').getByRole('button', { name: '查看详情' }).click()
  const dialog = page.getByRole('dialog', { name: '持仓查询记录' })
  await expect(dialog.getByRole('alert')).toContainText('已过期')
  await dialog.getByRole('button', { name: '重新读取' }).click()
  await expect(dialog).toContainText('本次查询无记录')
  await expect(dialog.getByRole('button', { name: '下一页' })).toBeDisabled()
  await page.screenshot({ path: '/tmp/axon-validation-mobile.png', fullPage: true })
  await page.keyboard.press('Escape')
  await page.locator('[data-validation-step="query_account"]').getByRole('button', { name: '查看详情' }).click()
  await expect(page.getByRole('dialog', { name: '资金查询记录' })).toContainText('未返回')
})
