import { expect, test, type Page } from '@playwright/test'

const algorithmDirectory = '/home/ichika/my-axon-algorithms/src/my_axon_algorithms'

async function setup(page: Page, directories: string[] = []) {
  const saves: Record<string, unknown>[] = []
  await page.route('**/api/v1/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/init/status')) return route.fulfill({ json: {
      configured: true,
      values: {
        environment: 'local', app_log_dir: 'logs', axile_log_rotation: '1 day',
        algorithm_modules: ['existing.algorithm'], algorithm_directories: directories,
      },
    } })
    if (path.endsWith('/utils/directories')) return route.fulfill({ json: {
      path: algorithmDirectory, parent: '/home/ichika/my-axon-algorithms/src', entries: [],
    } })
    if (path.endsWith('/init/save')) {
      saves.push(route.request().postDataJSON())
      return route.fulfill({ status: 500, json: { detail: '测试不重启服务' } })
    }
    if (path.endsWith('/account/dashboard') || path.endsWith('/portfolio/')) return route.fulfill({ json: { data: [] } })
    if (path.endsWith('/health-check/')) return route.fulfill({ json: true })
    if (path.endsWith('/events/stream')) return route.fulfill({ contentType: 'text/event-stream', body: ': connected\n\n' })
    return route.fulfill({ json: [] })
  })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/settings/advanced')
  return saves
}

test('选择目录即可添加，去重、移除、取消及保存保留已有模块配置', async ({ page }) => {
  const saves = await setup(page)
  const modules = page.getByRole('textbox', { name: 'Python 模块名', includeHidden: true })
  await expect(page.getByRole('button', { name: /按模块名添加/ })).toHaveAttribute('aria-expanded', 'false')
  await expect.poll(() => modules.evaluate((element) => element.closest('[inert]')?.getBoundingClientRect().height)).toBe(0)
  await modules.evaluate((element) => (element as HTMLInputElement).focus())
  await expect(modules).not.toBeFocused()
  for (let i = 0; i < 2; i++) {
    await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
    await page.getByRole('button', { name: '使用此目录', exact: true }).click()
    await expect(page.getByRole('dialog', { name: '选择目录', exact: true })).toHaveCount(0)
  }
  const list = page.getByRole('list', { name: '已添加的算法目录' })
  await expect(list.getByRole('listitem')).toHaveCount(1)
  await expect(list).toContainText(algorithmDirectory)
  await list.getByRole('button', { name: /移除算法目录/ }).click()
  await expect(list.getByRole('listitem')).toHaveCount(0)
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  await page.getByRole('button', { name: '使用此目录', exact: true }).click()
  await page.getByRole('button', { name: '取消修改', exact: true }).click()
  await expect(list.getByRole('listitem')).toHaveCount(0)
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  await page.getByRole('button', { name: '使用此目录', exact: true }).click()
  await page.getByRole('button', { name: '保存并重启', exact: true }).click()
  await page.getByRole('dialog').filter({ hasText: '保存并重启服务' }).getByRole('button', { name: '保存并重启', exact: true }).click()
  await expect.poll(() => saves.length).toBe(1)
  expect(saves[0].algorithm_directories).toEqual([algorithmDirectory])
  expect(saves[0].algorithm_modules).toEqual(['existing.algorithm'])
})

test('明暗主题和窄屏保持紧凑，高级模块按需展开', async ({ page }, testInfo) => {
  await setup(page, [algorithmDirectory])
  for (const dark of [false, true]) {
    await page.evaluate((value) => document.documentElement.classList.toggle('dark', value), dark)
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 800 })
      const button = page.getByRole('button', { name: '添加算法目录', exact: true })
      await button.scrollIntoViewIfNeeded()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`algorithms-${dark ? 'dark' : 'light'}-${width}.png`), animations: 'disabled' })
    }
  }
  await page.getByRole('button', { name: /按模块名添加/ }).click()
  await expect(page.getByRole('textbox', { name: 'Python 模块名' })).toBeVisible()
  await expect(page.getByText('existing.algorithm', { exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: 'Python 模块名' }).fill('another.algorithm')
  await page.getByRole('textbox', { name: 'Python 模块名' }).press('Enter')
  await expect(page.getByText('another.algorithm', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: /按模块名添加/ }).click()
  await expect(page.getByRole('textbox', { name: 'Python 模块名' })).toHaveCount(0)
})
