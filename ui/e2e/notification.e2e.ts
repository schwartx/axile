import { expect, test, type Page } from '@playwright/test'

const template = 'def notify(context):\n    pass\n'
const custom = 'def notify(context):\n    print(context)\n'
async function setup(page: Page, status: 'none' | 'default' | 'function', configured = true) {
  let account = {
    id: 1, name: '测试账户', remark: '', trade_channel: 'ctp', market: '期货', portfolio_id: null,
    forbidden_symbols: null, risk_symbols: null, weight_precision: 0.01, execution_timeout: 60,
    long_leverage: 1, short_leverage: 1, write_empty_record: 0,
    execution_notification_status: status, execution_notification_code: status === 'none' ? null : status === 'default' ? template : custom,
    feishu_configured: configured,
  }
  const patches: Record<string, unknown>[] = []
  const trials: Record<string, unknown>[] = []
  const control = { failTemplate: false, failSave: false, templateDelay: 0, saveDelay: 0, trialDelay: 0, templateReads: 0 }
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url()).pathname
    if (url.endsWith('/notification/default')) {
      control.templateReads += 1
      if (control.templateDelay) await new Promise((resolve) => setTimeout(resolve, control.templateDelay))
      await route.fulfill({ status: control.failTemplate ? 500 : 200, json: control.failTemplate ? { detail: '模板读取失败' } : { code: template } })
    } else if (url.endsWith('/notification/test') || url.endsWith('/feishu/test')) {
      trials.push(route.request().postDataJSON())
      if (control.trialDelay) await new Promise((resolve) => setTimeout(resolve, control.trialDelay))
      await route.fulfill({ json: { ok: true, message: '样例函数运行成功' } })
    } else if (route.request().method() === 'PATCH') {
      const patch = route.request().postDataJSON()
      patches.push(patch)
      if (control.saveDelay) await new Promise((resolve) => setTimeout(resolve, control.saveDelay))
      if (control.failSave) return route.fulfill({ status: 500, json: { detail: '保存失败' } })
      account = { ...account, ...patch }
      account.execution_notification_status = !account.execution_notification_code ? 'none' : account.execution_notification_code.trim() === template.trim() ? 'default' : 'function'
      await route.fulfill({ json: account })
    } else if (url.endsWith('/account/1')) await route.fulfill({ json: account })
    else await route.fulfill({ json: [] })
  })
  return { patches, trials, control }
}

test('自定义重置、取消、失败重试及同一次保存保留其他草稿', async ({ page }) => {
  const { patches, control } = await setup(page, 'function', false)
  await page.goto('/e2e/notification.html')
  const webhook = page.getByPlaceholder('可粘贴整条 webhook 链接')
  await expect(page.getByRole('link', { name: '编辑与测试' })).toBeVisible()
  await expect.poll(() => webhook.evaluate((element) => element.closest('[inert]')?.getBoundingClientRect().height)).toBe(0)
  await expect(webhook.locator('xpath=ancestor::*[@inert]')).toHaveCount(1)
  await webhook.evaluate((element) => (element as HTMLInputElement).focus())
  await expect(webhook).not.toBeFocused()
  await page.locator('input').first().fill('新名称')
  control.failTemplate = true
  await page.getByRole('button', { name: '重置为默认' }).click()
  await expect(page.getByText('默认模板读取失败', { exact: true })).toBeVisible()
  await expect(page.locator('input').first()).toHaveValue('新名称')
  control.failTemplate = false
  await page.getByRole('button', { name: '重置为默认' }).click()
  await expect(webhook).toBeVisible()
  await expect(page.getByRole('link', { name: '编辑与测试' })).toHaveCount(0)
  await expect(page.getByRole('link', { name: /高级设置/ })).toBeVisible()
  await expect(page.getByText('执行通知：自定义函数 → 默认通知', { exact: false }).first()).toBeVisible()
  expect(patches).toHaveLength(0)
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await expect.poll(() => webhook.evaluate((element) => element.closest('[inert]')?.getBoundingClientRect().height)).toBe(0)
  await expect(page.locator('input').first()).toHaveValue('测试账户')
  await page.locator('input').first().fill('新名称')
  await page.getByRole('button', { name: '重置为默认' }).click()
  control.failSave = true
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByText('保存失败', { exact: true }).first()).toBeVisible()
  await expect(webhook).toBeVisible()
  control.failSave = false
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => patches.length).toBe(2)
  expect(patches[1]).toEqual({ name: '新名称', execution_notification_code: template })
})

test('通知关闭但已有 Key 时重新配置显式启用，测试不保存', async ({ page }) => {
  const { patches, trials } = await setup(page, 'none')
  await page.goto('/e2e/notification.html')
  await expect(page.getByRole('link', { name: '编辑与测试' })).toHaveCount(0)
  await page.getByPlaceholder('已配置 · 留空保持不变').fill('https://open.feishu.cn/open-apis/bot/v2/hook/new-key')
  await page.getByRole('button', { name: '测试推送' }).click()
  await expect.poll(() => trials.length).toBe(1)
  expect(trials[0]).toEqual({ feishu_key: 'new-key' })
  expect(patches).toHaveLength(0)
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => patches.length).toBe(1)
  expect(patches[0]).toEqual({ feishu_key: 'new-key', execution_notification_code: template })
})

test('重置后清除及撤销保留通知草稿，明确清除关闭通知', async ({ page }) => {
  const { patches } = await setup(page, 'function')
  await page.goto('/e2e/notification.html')
  await page.getByRole('button', { name: '重置为默认' }).click()
  await page.getByRole('button', { name: '清除 Webhook' }).click()
  await page.getByRole('button', { name: '撤销清除' }).click()
  await expect(page.getByText('执行通知：自定义函数 → 默认通知', { exact: false }).first()).toBeVisible()
  await page.getByRole('button', { name: '清除 Webhook' }).click()
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => patches.length).toBe(1)
  expect(patches[0]).toEqual({ feishu_key: null, execution_notification_code: null })
})

for (const status of ['none', 'default', 'function'] as const) {
  test(`函数编辑页 ${status} 无 Webhook，试跑提交源码和事件类型、保存只提交源码`, async ({ page }) => {
    const { patches, trials } = await setup(page, status)
    await page.goto('/e2e/notification.html?editor=1')
    await expect(page.getByRole('button', { name: '试跑函数' })).toBeVisible()
    await expect(page.getByRole('textbox', { name: /Webhook/ })).toHaveCount(0)
    await expect(page.getByPlaceholder(/Webhook|webhook|Key/)).toHaveCount(0)
    await page.getByRole('button', { name: '试跑函数' }).click()
    await expect.poll(() => trials.length).toBe(1)
    expect(Object.keys(trials[0]).sort()).toEqual(['code', 'event_type'])
    expect(trials[0].event_type).toBe('execution.finished')
    expect(patches).toHaveLength(0)
    if (status !== 'none') await page.getByRole('button', { name: '清空函数' }).click()
    await page.getByRole('button', { name: '保存', exact: true }).click()
    await expect.poll(() => patches.length).toBe(1)
    expect(patches[0]).toEqual({ execution_notification_code: status === 'none' ? template : null })
  })
}

test('读取和保存期间禁止重复操作', async ({ page }) => {
  const { patches, control } = await setup(page, 'function')
  control.templateDelay = 500
  control.saveDelay = 500
  await page.goto('/e2e/notification.html')
  await page.getByRole('button', { name: '重置为默认' }).click()
  await expect(page.getByRole('button', { name: '读取中…' })).toBeDisabled()
  await expect(page.getByRole('link', { name: /高级设置/ })).toBeVisible()
  expect(control.templateReads).toBe(1)
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('button', { name: '保存中…' })).toBeDisabled()
  await expect(page.locator('input').first()).toBeDisabled()
  await expect(page.getByRole('button', { name: '保存中…' })).toHaveCount(0)
  expect(patches).toHaveLength(1)
})

test('编辑页模板失败可重试，重置只修改草稿并使试跑过期', async ({ page }) => {
  const { patches, control } = await setup(page, 'function')
  control.failTemplate = true
  await page.goto('/e2e/notification.html?editor=1')
  await expect(page.getByRole('button', { name: /重试/ })).toBeVisible()
  expect(patches).toHaveLength(0)
  control.failTemplate = false
  await page.getByRole('button', { name: /重试/ }).click()
  await page.getByRole('button', { name: '试跑函数' }).click()
  await expect(page.getByText('样例函数运行成功')).toBeVisible()
  await page.getByRole('button', { name: '重置为默认' }).click()
  await expect(page.getByText(/当前草稿未保存/)).toBeVisible()
  await expect(page.getByText("代码已改 · 结果为上次试跑").last()).toBeVisible()
  expect(patches).toHaveLength(0)
})

async function editCode(page: Page, code: string) {
  await page.locator('.cm-content').first().click()
  await page.keyboard.press('ControlOrMeta+a')
  await page.keyboard.insertText(code)
}

test('通知公共保存快捷键、失败重试、还原和保存后继续编辑', async ({ page }) => {
  const { patches, control } = await setup(page, 'function')
  await page.goto('/e2e/notification.html?editor=1')
  await expect(page.getByRole('button', { name: '试跑函数' })).toBeVisible()
  await editCode(page, 'def notify(context):\n    print("first")\n')
  control.failSave = true
  await page.keyboard.press('ControlOrMeta+s')
  await expect(page.locator('footer').getByText('保存失败', { exact: true }).last()).toBeVisible()
  control.failSave = false
  control.saveDelay = 400
  await page.keyboard.press('ControlOrMeta+s')
  await page.keyboard.press('ControlOrMeta+s')
  await expect.poll(() => patches.length).toBe(2)
  await expect(page.locator('footer').getByText('已保存', { exact: true }).last()).toBeVisible()
  await expect(page.getByRole('button', { name: '试跑函数' })).toBeVisible()
  await page.keyboard.press('ControlOrMeta+s')
  expect(patches).toHaveLength(2)
  await editCode(page, 'def notify(context):\n    print("second")\n')
  await page.getByRole('button', { name: '还原', exact: true }).click()
  await expect(page.locator('.cm-content').first()).toContainText('first')
  await expect(page.locator('footer').getByText('已保存', { exact: true }).last()).toBeVisible()
})

test('保存中的新修改保留为未保存，还原到提交版本', async ({ page }) => {
  const { patches, control } = await setup(page, 'function')
  control.saveDelay = 500
  await page.goto('/e2e/notification.html?editor=1')
  await editCode(page, 'def notify(context):\n    print("submitted")\n')
  await page.keyboard.press('ControlOrMeta+s')
  await expect.poll(() => patches.length).toBe(1)
  await editCode(page, 'def notify(context):\n    print("later edit")\n')
  await expect(page.locator('footer').getByText('未保存', { exact: true }).last()).toBeVisible()
  await page.getByRole('button', { name: '还原', exact: true }).click()
  await expect(page.locator('.cm-content').first()).toContainText('submitted')
})

test('组合接入公共保存、还原和分栏记忆', async ({ page }) => {
  let portfolio = { id: 1, name: '测试组合', custom_calc_py_code: 'def calculate_portfolio(context):\n    return {}\n' }
  const patches: Record<string, unknown>[] = []
  const trials: Record<string, unknown>[] = []
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url()).pathname
    if (url.endsWith('/validate_custom_calc')) {
      trials.push(route.request().postDataJSON())
      await new Promise((resolve) => setTimeout(resolve, 300))
      await route.fulfill({ json: { valid: true, target: { rb2610: 0.5 }, error: null, error_line: null } })
    } else if (route.request().method() === 'PATCH') {
      const patch = route.request().postDataJSON()
      patches.push(patch)
      portfolio = { ...portfolio, ...patch }
      await route.fulfill({ json: portfolio })
    } else if (url.endsWith('/portfolio/1')) await route.fulfill({ json: portfolio })
    else if (url.includes('target_snapshot')) await route.fulfill({ json: { weights: {}, calculated_at: null } })
    else await route.fulfill({ json: [] })
  })
  await page.goto('/e2e/notification.html?portfolio=1')
  await editCode(page, 'def calculate_portfolio(context):\n    return {"rb2610": 0.5}\n')
  await page.keyboard.press('ControlOrMeta+Enter')
  await page.keyboard.press('ControlOrMeta+Enter')
  await expect.poll(() => trials.length).toBe(1)
  await expect(page.getByText('函数执行成功。', { exact: true })).toBeVisible()
  expect(patches).toHaveLength(0)
  await page.keyboard.press('ControlOrMeta+s')
  await expect.poll(() => patches.length).toBe(1)
  await expect(page.locator('footer').getByText('已保存', { exact: true }).last()).toBeVisible()
  await editCode(page, 'def calculate_portfolio(context):\n    return {}\n')
  await expect(page.getByText('代码已修改，以下为旧结果', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '还原', exact: true }).click()
  await expect(page.locator('.cm-content').first()).toContainText('rb2610')
  const splitter = page.getByRole('separator', { name: '调整运行检查器宽度' })
  await splitter.focus()
  await page.keyboard.press('ArrowRight')
  await expect(splitter).toHaveAttribute('aria-valuenow', '336')
  await page.reload()
  await expect(splitter).toHaveAttribute('aria-valuenow', '336')
  await splitter.dblclick()
  await expect(splitter).toHaveAttribute('aria-valuenow', '320')
})

test('系统通知共用试跑、过期状态、保存快捷键与还原', async ({ page }) => {
  const trials: Record<string, unknown>[] = []
  const saves: Record<string, unknown>[] = []
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url()).pathname
    if (url.endsWith('/function/test') || url.endsWith('/init/execution-alert')) {
      if (url.endsWith('/function/test')) {
        trials.push(route.request().postDataJSON())
        await new Promise((resolve) => setTimeout(resolve, 300))
      } else saves.push(route.request().postDataJSON())
      await route.fulfill({ json: { ok: true, message: '样例运行成功' } })
    } else if (url.endsWith('/init/status')) await route.fulfill({ json: { configured: true, values: { exe_err_feishu_configured: true, system_execution_notification_mode: 'function', system_execution_notification_code: 'def notify(context):\n    pass\n', environment: 'local', app_log_dir: './logs', axile_log_rotation: '1 day', algorithm_modules: [], algorithm_directories: [] } } })
    else if (url.endsWith('/function/default')) await route.fulfill({ json: { code: template } })
    else await route.fulfill({ json: [] })
  })
  await page.goto('/e2e/notification.html?system=1')
  await expect(page.getByRole('button', { name: '试跑函数' })).toBeVisible()
  await editCode(page, 'def notify(context):\n    print("system")\n')
  await page.keyboard.press('ControlOrMeta+Enter')
  await page.keyboard.press('ControlOrMeta+Enter')
  await expect.poll(() => trials.length).toBe(1)
  await expect(page.getByText('样例运行成功', { exact: true })).toBeVisible()
  await editCode(page, 'def notify(context):\n    print("changed")\n')
  await expect(page.getByText('代码已改 · 结果为上次试跑', { exact: true }).last()).toBeVisible()
  await page.keyboard.press('ControlOrMeta+s')
  await expect.poll(() => saves.length).toBe(1)
  expect(saves[0].system_execution_notification_code).toContain('changed')
  await expect(page.locator('footer').getByText('已保存', { exact: true }).last()).toBeVisible()
  await editCode(page, 'def notify(context):\n    pass\n')
  await page.getByRole('button', { name: '还原', exact: true }).click()
  await expect(page.locator('.cm-content').first()).toContainText('changed')
})

test('通知试跑按钮和快捷键共用禁用条件，空代码不试跑但允许关闭通知', async ({ page }) => {
  const { patches, trials, control } = await setup(page, 'function')
  control.trialDelay = 400
  await page.goto('/e2e/notification.html?editor=1')
  await editCode(page, 'def notify(context):\n    print("trial")\n')
  await page.keyboard.press('ControlOrMeta+Enter')
  await page.keyboard.press('ControlOrMeta+Enter')
  await page.keyboard.press('ControlOrMeta+s')
  await expect.poll(() => trials.length).toBe(1)
  expect(patches).toHaveLength(0)
  await expect(page.getByText('样例函数运行成功', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '清空函数' }).click()
  await page.locator('.cm-content').first().focus()
  await page.keyboard.press('ControlOrMeta+Enter')
  await expect(page.getByRole('button', { name: '试跑函数' })).toBeDisabled()
  expect(trials).toHaveLength(1)
  await page.keyboard.press('ControlOrMeta+s')
  await expect.poll(() => patches.length).toBe(1)
  expect(patches[0]).toEqual({ execution_notification_code: null })
})

async function setupSystem(page: Page) {
  let values = { exe_err_feishu_configured: true, system_execution_notification_mode: 'default', system_execution_notification_code: 'def notify(context):\n    print("custom")\n', environment: 'local', app_log_dir: './logs', axile_log_rotation: '1 day', algorithm_modules: [], algorithm_directories: [] }
  const patches: Record<string, unknown>[] = []
  const trials: Record<string, unknown>[] = []
  const result = { finished_at: '2026-09-30T10:01:00+08:00', mode: 'function', event_type: 'execution_timeout', execution_id: 'real-exec', account_id: 1, ok: false, error: 'real failure' }
  const control = { failSave: false, delaySave: 0 }
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url()).pathname
    if (url.endsWith('/init/status')) await route.fulfill({ json: { configured: true, values } })
    else if (url.endsWith('/function/default')) await route.fulfill({ json: { code: template } })
    else if (url.endsWith('/execution-alert/result')) await route.fulfill({ json: result })
    else if (url.endsWith('/function/test')) {
      trials.push(route.request().postDataJSON())
      await route.fulfill({ json: { ok: false, message: 'trial failed', error_line: 2 } })
    } else if (url.endsWith('/init/test-feishu')) {
      trials.push(route.request().postDataJSON())
      await route.fulfill({ json: { ok: true, message: 'default test succeeded' } })
    } else if (url.endsWith('/init/execution-alert')) {
      const patch = route.request().postDataJSON()
      patches.push(patch)
      await new Promise((resolve) => setTimeout(resolve, control.delaySave))
      if (control.failSave) await route.fulfill({ status: 500, json: { detail: 'disk full' } })
      else {
        values = { ...values, ...patch, exe_err_feishu_configured: patch.exe_err_feishu_key === null ? values.exe_err_feishu_configured : Boolean(patch.exe_err_feishu_key) }
        await route.fulfill({ json: { ok: true, message: 'saved' } })
      }
    } else await route.fulfill({ json: [] })
  })
  return { patches, trials, control }
}

test('系统配置使用已保存凭据测试；跨页保留草稿、切换模式保留源码', async ({ page }) => {
  const { patches, trials } = await setupSystem(page)
  await page.goto('/e2e/notification.html?system-settings=1')
  await expect(page.getByText('real failure')).toBeVisible()
  await page.getByRole('button', { name: '测试推送', exact: true }).click()
  await expect.poll(() => trials.length).toBe(1)
  expect(trials[0]).toEqual({ key: null })
  expect(patches).toHaveLength(0)
  await page.getByRole('textbox', { name: '系统飞书 Webhook' }).fill('draft-key')
  await expect(page.getByRole('button', { name: '默认', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '自定义', exact: true })).toHaveCount(0)
  await page.getByRole('link', { name: '高级设置 · 自定义执行通知函数' }).click()
  await page.getByRole('button', { name: '自定义', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '系统飞书 Webhook' })).toHaveValue('draft-key')
  await expect(page.locator('.cm-content').first()).toContainText('custom')
  await page.getByRole('button', { name: '执行超时', exact: true }).click()
  await page.getByRole('button', { name: '试跑函数', exact: true }).click()
  await expect.poll(() => trials.length).toBe(2)
  expect(trials[1]).toMatchObject({ key: 'draft-key', event_type: 'execution_timeout' })
  await expect(page.getByRole('button', { name: '定位到第 2 行' })).toBeVisible()
  await page.getByRole('button', { name: '额度预警', exact: true }).click()
  await page.getByRole('button', { name: '试跑函数', exact: true }).click()
  await expect.poll(() => trials.length).toBe(3)
  expect(trials[2]).toMatchObject({ key: 'draft-key', event_type: 'account_control.warning' })
  await page.getByRole('button', { name: '执行异常', exact: true }).click()
  await expect(page.getByText('代码已改 · 结果为上次试跑').last()).toBeVisible()
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => patches.length).toBe(1)
  expect(patches[0]).toMatchObject({ exe_err_feishu_key: 'draft-key', system_execution_notification_mode: 'function' })
  await expect(page.locator('footer').getByText('已保存', { exact: true }).last()).toBeVisible()
  await page.getByRole('button', { name: '默认', exact: true }).click()
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => patches.length).toBe(2)
  expect(patches[1]).toMatchObject({ exe_err_feishu_key: null, system_execution_notification_mode: 'default' })
  expect(patches[1].system_execution_notification_code).toBe(patches[0].system_execution_notification_code)
  await page.getByRole('link', { name: '返回系统告警' }).click()
  await expect(page.getByText('real failure')).toBeVisible()
})

test('系统凭据明确清除可还原，保存失败保留草稿，空自定义不保存', async ({ page }) => {
  const { patches, control } = await setupSystem(page)
  await page.goto('/e2e/notification.html?system=1')
  await expect(page.getByRole('button', { name: '试跑函数' })).toBeVisible()
  await page.getByRole('button', { name: '清除 Webhook', exact: true }).click()
  await page.getByRole('button', { name: '还原', exact: true }).click()
  await expect(page.getByRole('button', { name: '撤销清除' })).toHaveCount(0)
  await page.getByRole('button', { name: '清除 Webhook', exact: true }).click()
  control.failSave = true
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.locator('footer').getByText('保存失败', { exact: true }).last()).toBeVisible()
  expect(patches[0].exe_err_feishu_key).toBe('')
  await expect(page.getByRole('button', { name: '撤销清除' })).toBeVisible()
  control.failSave = false
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.locator('footer').getByText('已保存', { exact: true }).last()).toBeVisible()
  await page.getByRole('button', { name: '自定义', exact: true }).click()
  await page.getByRole('button', { name: '清空函数' }).click()
  await expect(page.getByRole('button', { name: '保存', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '试跑函数' })).toBeDisabled()
  await page.locator('.cm-content').first().focus()
  await page.keyboard.press('ControlOrMeta+s')
  expect(patches).toHaveLength(2)
})
