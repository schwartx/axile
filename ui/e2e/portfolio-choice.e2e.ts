import { expect, test, type Page } from '@playwright/test'

async function mockSnapshots(page: Page, fail = false) {
  const methods: string[] = []
  await page.route('**/portfolio/*/target_snapshot', async (route) => {
    methods.push(route.request().method())
    const id = Number(route.request().url().match(/portfolio\/(\d+)/)?.[1])
    if (id === 2 && fail) {
      fail = false
      await route.fulfill({ status: 503, json: { detail: '暂时无法读取' } })
      return
    }
    await route.fulfill({ json: {
      weights: id === 1 ? { 'SHFE.rb2610': 0.2, 'SHFE.au2612': -0.15, 'SHFE.cu2611': 0.1, 'DCE.i2611': 0.05 } : {},
      calculated_at: id === 2 ? null : '2026-10-13T14:30:00+08:00', source: 'manual', execution_id: null, context_account_id: null,
    } })
  })
  return methods
}

test('预览不改变选择，关闭归还焦点，弹窗内可以确认新选择', async ({ page }) => {
  const methods = await mockSnapshots(page)
  await page.goto('/e2e/portfolio-choice.html')
  const original = page.getByRole('button', { name: '选择组合：商品趋势', exact: true, includeHidden: true })
  await expect(original).toContainText('4 个目标品种')
  await expect(original).toContainText('SHFE.rb2610')
  await expect(original).toContainText('-15.0%')
  await expect(page.getByRole('button', { name: '选择组合：备选组合', exact: true })).toContainText('尚无目标持仓')
  await expect(page.getByRole('button', { name: '选择组合：空仓组合', exact: true })).toContainText('目标为空仓')
  await expect(original).toContainText('总敞口 50.0%')
  await expect(original).toContainText('净敞口 +20.0%')
  expect(await original.textContent()).not.toContain('🎯')
  expect(await original.textContent()).not.toContain('未填写组合说明')
  const before = await original.boundingBox()
  expect(before!.height).toBeLessThanOrEqual(96)
  const preview = page.getByRole('button', { name: '预览代码：备选组合' })
  await preview.click()
  const dialog = page.getByRole('dialog', { name: '备选组合' })
  await expect(dialog).toBeVisible()
  await expect(dialog.locator('.cm-content')).toHaveAttribute('aria-readonly', 'true')
  await expect(dialog.locator('.cm-lineNumbers')).toBeVisible()
  const sourceBefore = await dialog.locator('.cm-content').textContent()
  await dialog.locator('.cm-content').click()
  await page.keyboard.type('should_not_edit')
  expect(await dialog.locator('.cm-content').textContent()).toBe(sourceBefore)
  await expect(original).toHaveAttribute('aria-pressed', 'true')
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(preview).toBeFocused()
  expect(await original.boundingBox()).toEqual(before)
  await preview.click()
  await dialog.getByRole('button', { name: '选择此组合' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(original).toHaveAttribute('aria-pressed', 'false')
  await expect(page.getByRole('button', { name: '选择组合：备选组合', exact: true })).toHaveAttribute('aria-pressed', 'true')
  expect(methods.every((method) => method === 'GET')).toBe(true)
})

test('读取失败可重试，窄屏和长代码均在各自容器内滚动', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockSnapshots(page, true)
  await page.goto('/e2e/portfolio-choice.html')
  const choice = page.getByRole('button', { name: '选择组合：备选组合', exact: true })
  await expect(choice).toContainText('目标持仓读取失败')
  await page.getByRole('button', { name: '重试：备选组合 目标持仓读取失败', exact: true }).click()
  await expect(choice).toContainText('尚无目标持仓')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  const card = page.getByRole('button', { name: '选择组合：商品趋势', exact: true })
  await expect(card).toContainText('4 个品种')
  await expect(card.locator('span').filter({ hasText: /^\+\d$/ }).last()).toBeVisible()
  expect((await card.boundingBox())!.height).toBeLessThanOrEqual(96)
  await page.getByRole('button', { name: '预览代码：备选组合' }).click()
  const dialog = page.getByRole('dialog', { name: '备选组合' })
  await expect(dialog.locator('.cm-scroller')).toBeVisible()
  expect(await dialog.locator('.cm-scroller').evaluate((node) => node.scrollHeight > node.clientHeight)).toBe(true)
  await dialog.getByRole('button', { name: '关闭代码预览' }).click()
  await expect(dialog).toHaveCount(0)
})

test('弹窗关闭等待过渡，减少动态效果时立即关闭', async ({ page }) => {
  await mockSnapshots(page)
  await page.goto('/e2e/portfolio-choice.html')
  const preview = page.getByRole('button', { name: '预览代码：备选组合' })
  await preview.click()
  const dialog = page.getByRole('dialog', { name: '备选组合' })
  await expect(dialog).toBeVisible()
  await expect.poll(() => dialog.evaluate((node) => getComputedStyle(node).opacity)).toBe('1')
  await dialog.evaluate((node) => {
    // 锁住关闭过渡，确认组件仍处于原生模态层，未被直接卸载。
    node.addEventListener('transitionrun', () => {
      if (node.hasAttribute('data-closing')) node.getAnimations().forEach((animation) => animation.pause())
    })
  })
  await dialog.getByRole('button', { name: '关闭代码预览' }).click()
  await expect(dialog).toHaveAttribute('data-closing', 'true')
  await expect(dialog).toHaveAttribute('open', '')
  await dialog.evaluate((node) => node.getAnimations().forEach((animation) => animation.finish()))
  await expect(dialog).toHaveCount(0)
  await expect(preview).toBeFocused()

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await preview.click()
  await expect(dialog).toBeVisible()
  expect(await dialog.evaluate((node) => getComputedStyle(node).transitionDuration)).toBe('0s')
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(preview).toBeFocused()
})
