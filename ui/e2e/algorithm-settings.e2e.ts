import { expect, test, type Page } from '@playwright/test'
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const algorithmDirectory = '/home/ichika/my-axon-algorithms/src/my_axon_algorithms'

async function setup(page: Page, directories: string[] = [], nativeDiscovery = false) {
  const saves: Record<string, unknown>[] = []
  const scans: string[] = []
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
    if (path.endsWith('/algorithms/discover')) {
      if (nativeDiscovery) return route.continue()
      const requested = new URL(route.request().url()).searchParams.get('path') ?? algorithmDirectory
      scans.push(requested)
      return route.fulfill({ json: {
        path: requested, parent: '/home/ichika/my-axon-algorithms/src', entries: [],
        discovery: { files: 3, algorithms: [{ name: 'PREV-VWAP-OPEN', function: 'prev_vwap_open', file: 'prev_vwap_open.py' }], issues: [], can_use: true },
      } })
    }
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
  return { saves, scans }
}

test('选择目录即可添加，去重、移除、取消及保存保留已有模块配置', async ({ page }) => {
  const { saves, scans } = await setup(page)
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
  await expect(list).toContainText('PREV-VWAP-OPEN')
  expect(scans).toHaveLength(2)
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

test('真实 Rust API 只扫描当前层，没有算法时不能选择目录', async ({ page }, testInfo) => {
  const root = mkdtempSync(join(tmpdir(), 'axile-browser-algorithms-'))
  const scripts = join(root, 'scripts')
  const sentinel = join(root, 'executed.txt')
  mkdirSync(scripts)
  writeFileSync(join(scripts, 'opening.py'), `from pathlib import Path\nPath(${JSON.stringify(sentinel)}).write_text('executed')\n@register_algorithm('BROWSER-OPEN')\ndef opening(e, i): pass\n`)
  try {
    await setup(page, [], true)
    await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
    const location = page.getByRole('textbox', { name: '目录路径' })
    await location.fill(root)
    await location.press('Enter')
    await expect(page.getByText('当前目录没有发现算法', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '使用此目录', exact: true })).toBeDisabled()
    await page.getByRole('button', { name: 'scripts', exact: true }).click()
    await expect(page.getByRole('region', { name: '当前目录的算法' })).toContainText('BROWSER-OPEN')
    await expect(page.getByRole('button', { name: '使用此目录', exact: true })).toBeEnabled()
    await page.screenshot({ path: testInfo.outputPath('native-algorithm-preview.png') })
    await location.fill(root)
    await expect(page.getByRole('button', { name: '使用此目录', exact: true })).toBeDisabled()
    await location.fill(scripts)
    await page.getByRole('button', { name: '使用此目录', exact: true }).click()
    await expect(page.getByRole('list', { name: '已添加的算法目录' })).toContainText('BROWSER-OPEN')
    expect(existsSync(sentinel)).toBe(false)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('切换目录读取期间及读取失败时，不能使用上一个目录的预览', async ({ page }) => {
  await setup(page)
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  await expect(page.getByRole('button', { name: '使用此目录', exact: true })).toBeEnabled()
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/v1/algorithms/discover?path=%2Fmissing', async route => {
    await pending
    await route.fulfill({ status: 404, json: { detail: '目录不存在' } })
  })
  await page.getByRole('textbox', { name: '目录路径' }).fill('/missing')
  await page.getByRole('textbox', { name: '目录路径' }).press('Enter')
  await expect(page.getByRole('button', { name: '使用此目录', exact: true })).toBeDisabled()
  release()
  await expect(page.getByText('目录不存在', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '使用此目录', exact: true })).toBeDisabled()
})

test('初次读取结果不能覆盖用户正在输入的路径', async ({ page }) => {
  await setup(page)
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/v1/algorithms/discover', async route => {
    await pending
    await route.fulfill({ json: { path: '/initial', parent: '/', entries: [],
      discovery: { files: 1, algorithms: [{ name: 'INITIAL', function: 'run', file: 'a.py' }], issues: [], can_use: true },
    } })
  })
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  await page.getByRole('textbox', { name: '目录路径' }).fill('/user/typed')
  release()
  await expect(page.getByRole('region', { name: '当前目录的算法' })).toContainText('INITIAL')
  await expect(page.getByRole('textbox', { name: '目录路径' })).toHaveValue('/user/typed')
  await expect(page.getByRole('button', { name: '使用此目录', exact: true })).toBeDisabled()
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
