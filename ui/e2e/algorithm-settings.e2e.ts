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

async function editDirectoryPath(page: Page) {
  const edit = page.getByRole('button', { name: '编辑目录路径', exact: true })
  if (await edit.isVisible()) await edit.click()
  return page.getByRole('textbox', { name: '目录路径' })
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
    await page.getByRole('button', { name: '添加此目录', exact: true }).click()
    await expect(page.getByRole('dialog', { name: '选择算法目录', exact: true })).toHaveCount(0)
  }
  const list = page.getByRole('list', { name: '已添加的算法目录' })
  await expect(list.getByRole('listitem')).toHaveCount(1)
  await expect(list).toContainText(algorithmDirectory)
  await expect(list).toContainText('PREV-VWAP-OPEN')
  expect(scans).toHaveLength(2)
  await list.getByRole('button', { name: /移除算法目录/ }).click()
  await expect(list.getByRole('listitem')).toHaveCount(0)
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  await page.getByRole('button', { name: '添加此目录', exact: true }).click()
  await page.getByRole('button', { name: '取消修改', exact: true }).click()
  await expect(list.getByRole('listitem')).toHaveCount(0)
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  await page.getByRole('button', { name: '添加此目录', exact: true }).click()
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
    const location = await editDirectoryPath(page)
    await editDirectoryPath(page)
    await location.fill(root)
    await location.press('Enter')
    await expect(page.getByText('当前目录没有发现算法', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '添加此目录', exact: true })).toBeDisabled()
    await page.getByRole('button', { name: 'scripts', exact: true }).click()
    await expect(page.getByRole('region', { name: '当前目录的算法' })).toContainText('BROWSER-OPEN')
    await expect(page.getByRole('button', { name: '添加此目录', exact: true })).toBeEnabled()
    await page.evaluate(() => document.documentElement.classList.add('dark'))
    await expect(page.getByRole('dialog', { name: '选择算法目录' })).toHaveCSS('background-color', 'rgb(20, 26, 34)')
    await page.screenshot({ path: testInfo.outputPath('native-algorithm-preview.png') })
    await editDirectoryPath(page)
    await location.fill(root)
    await expect(page.getByRole('button', { name: '添加此目录', exact: true })).toBeDisabled()
    await location.fill(scripts)
    await page.getByRole('button', { name: '添加此目录', exact: true }).click()
    await expect(page.getByRole('list', { name: '已添加的算法目录' })).toContainText('BROWSER-OPEN')
    expect(existsSync(sentinel)).toBe(false)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('切换目录读取期间及读取失败时，不能使用上一个目录的预览', async ({ page }) => {
  await setup(page)
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  await expect(page.getByRole('button', { name: '添加此目录', exact: true })).toBeEnabled()
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/v1/algorithms/discover?path=%2Fmissing', async route => {
    await pending
    await route.fulfill({ status: 404, json: { detail: '目录不存在' } })
  })
  await (await editDirectoryPath(page)).fill('/missing')
  await page.getByRole('textbox', { name: '目录路径' }).press('Enter')
  await expect(page.getByRole('button', { name: '添加此目录', exact: true })).toBeDisabled()
  release()
  await expect(page.getByText('目录不存在', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '添加此目录', exact: true })).toBeDisabled()
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
  await (await editDirectoryPath(page)).fill('/user/typed')
  release()
  await expect(page.getByRole('region', { name: '当前目录的算法' })).toContainText('INITIAL')
  await expect(page.getByRole('textbox', { name: '目录路径' })).toHaveValue('/user/typed')
  await expect(page.getByRole('button', { name: '添加此目录', exact: true })).toBeDisabled()
})

test('左右分栏独立滚动、隐藏项与目录键盘导航', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  const { scans } = await setup(page)
  let requests = 0
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  await page.route('**/api/v1/algorithms/discover**', async route => {
    requests++
    const path = new URL(route.request().url()).searchParams.get('path') ?? algorithmDirectory
    const child = path.endsWith('/folder-0')
    if (child) await pending
    await route.fulfill({ json: {
      path, parent: algorithmDirectory,
      entries: child ? [] : [{ name: '.git', path: `${path}/.git` }, ...Array.from({ length: 70 }, (_, i) => ({ name: `folder-${i}`, path: `${path}/folder-${i}` }))],
      discovery: { files: 70, algorithms: Array.from({ length: child ? 1 : 70 }, (_, i) => ({ name: child ? 'AFTER-NAVIGATION' : `ALGO-${i}`, function: `run_${i}`, file: `algorithm_${i}.py` })), issues: [], can_use: true },
    } })
  })
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '选择算法目录', exact: true })
  const navigation = page.getByRole('region', { name: '目录导航' })
  const preview = page.getByRole('region', { name: '当前目录的算法' })
  await expect(preview).toContainText('ALGO-0')
  await expect(navigation.getByRole('button', { name: '.git', exact: true })).toHaveCount(0)
  const leftScroll = navigation.locator('div.overflow-y-auto')
  const rightScroll = preview.locator('div.overflow-y-auto')
  await leftScroll.evaluate(element => { element.scrollTop = element.scrollHeight })
  expect(await rightScroll.evaluate(element => element.scrollTop)).toBe(0)
  await expect(preview.getByText('ALGO-0', { exact: true })).toBeVisible()
  const leftTop = await leftScroll.evaluate(element => element.scrollTop)
  await rightScroll.evaluate(element => { element.scrollTop = element.scrollHeight })
  expect(await leftScroll.evaluate(element => element.scrollTop)).toBe(leftTop)
  await leftScroll.evaluate(element => { element.scrollTop = 0 })
  const count = requests
  await page.getByRole('button', { name: '显示隐藏目录', exact: true }).click()
  await expect(navigation.getByRole('button', { name: '.git', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '显示隐藏目录', exact: true }).click()
  expect(requests).toBe(count)
  for (const dark of [false, true]) {
    await page.evaluate(value => document.documentElement.classList.toggle('dark', value), dark)
    await expect(dialog).toHaveCSS('background-color', dark ? 'rgb(20, 26, 34)' : 'rgb(255, 255, 255)')
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 800 })
    const a = (await navigation.boundingBox())!
    const b = (await preview.boundingBox())!
    expect(b.x).toBeGreaterThanOrEqual(a.x + a.width - 1)
    expect(Math.abs(a.y - b.y)).toBeLessThan(1)
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`two-pane-${dark ? 'dark' : 'light'}-${width}.png`) })
  }
  }
  await page.setViewportSize({ width: 1280, height: 800 })
  await navigation.locator('[data-current-directory]').focus()
  await page.keyboard.press('ArrowDown')
  await expect(navigation.getByRole('button', { name: 'folder-0', exact: true })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(preview.getByRole('status')).toContainText('正在检查目录…')
  await expect(preview).toHaveAttribute('aria-busy', 'true')
  await expect(navigation.locator('[aria-current="location"]')).toHaveCount(1)
  await expect(page.getByRole('button', { name: '添加此目录', exact: true })).toBeDisabled()
  release()
  await expect(preview).toContainText('AFTER-NAVIGATION')
  await expect(navigation.locator('[data-current-directory]')).toBeFocused()
  await page.keyboard.press('Alt+ArrowUp')
  await expect(preview).toContainText('ALGO-0')
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('button', { name: '添加算法目录', exact: true })).toBeFocused()
  expect(scans).toHaveLength(0)
})

test('日志目录保持普通选择流程，不扫描算法', async ({ page }) => {
  const { scans } = await setup(page)
  await page.getByRole('button', { name: '选择目录', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '选择目录', exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: '当前目录的算法' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '使用此目录', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '使用此目录', exact: true }).click()
  expect(scans).toHaveLength(0)
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

for (const motion of ['no-preference', 'reduce', 'unsupported'] as const) {
  test(`目录连续移动、返回恢复滚动与焦点：${motion}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    await setup(page)
    await page.emulateMedia({ reducedMotion: motion === 'reduce' ? 'reduce' : 'no-preference' })
    await page.route('**/api/v1/algorithms/discover**', route => {
      const path = new URL(route.request().url()).searchParams.get('path') ?? algorithmDirectory
      const root = path === algorithmDirectory
      return route.fulfill({ json: {
        path, parent: root ? '/home/ichika/my-axon-algorithms/src' : path.slice(0, path.lastIndexOf('/')),
        entries: root ? Array.from({ length: 60 }, (_, i) => ({ name: `folder-${i}`, path: `${path}/folder-${i}` })) : [{ name: 'nested', path: `${path}/nested` }],
        discovery: { files: 1, algorithms: [{ name: root ? 'ROOT' : path.endsWith('/nested') ? 'NESTED' : 'CHILD', function: 'run', file: 'run.py' }], issues: [], can_use: true },
      } })
    })
    await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
    const navigation = page.getByRole('region', { name: '目录导航' })
    const preview = page.getByRole('region', { name: '当前目录的算法' })
    await expect(preview).toContainText('ROOT')
    await page.evaluate(unsupported => {
      const start = document.startViewTransition.bind(document)
      document.documentElement.dataset.transitions = '[]'
      document.documentElement.dataset.skips = '0'
      if (unsupported) { Object.defineProperty(document, 'startViewTransition', { value: undefined, configurable: true }); return }
      document.startViewTransition = options => {
        const source = document.querySelector<HTMLElement>('[style*="algorithm-directory-row"]')
        const record = { source: source?.dataset.directoryPath, from: source?.getBoundingClientRect().top, destination: undefined as string | undefined, to: 0, animated: false }
        const transition = start(options)
        const skip = transition.skipTransition.bind(transition)
        transition.skipTransition = () => { document.documentElement.dataset.skips = String(Number(document.documentElement.dataset.skips) + 1); skip() }
        void transition.ready.then(() => {
          const destination = document.querySelector<HTMLElement>('[style*="algorithm-directory-row"]')
          record.destination = destination?.dataset.directoryPath
          record.to = destination?.getBoundingClientRect().top ?? 0
          const animations = document.getAnimations().filter(animation => (animation.effect as KeyframeEffect | null)?.pseudoElement?.includes('view-transition'))
          record.animated = animations.some(animation => (animation.effect as KeyframeEffect).pseudoElement?.includes('group(algorithm-directory-row)'))
          if (document.documentElement.dataset.holdMotion === 'true') animations.forEach(animation => { animation.pause(); animation.currentTime = 90 })
          const records = JSON.parse(document.documentElement.dataset.transitions!)
          records.push(record)
          document.documentElement.dataset.transitions = JSON.stringify(records)
        }).catch(() => {})
        return transition
      }
    }, motion === 'unsupported')
    const child = navigation.getByRole('button', { name: 'folder-20', exact: true })
    await child.scrollIntoViewIfNeeded()
    const scroll = navigation.locator('.directory-browser-list')
    const position = await scroll.evaluate(element => element.scrollTop)
    await child.click()
    await expect(preview).toContainText('CHILD')
    if (motion === 'no-preference') {
      await expect.poll(() => page.evaluate(() => JSON.parse(document.documentElement.dataset.transitions!).length)).toBe(1)
      const [record] = await page.evaluate(() => JSON.parse(document.documentElement.dataset.transitions!))
      expect(record.source).toBe(`${algorithmDirectory}/folder-20`)
      expect(record.destination).toBe(record.source)
      expect(record.from - record.to).toBeGreaterThan(30)
      expect(record.animated).toBe(true)
    }
    await expect(navigation.locator('[data-current-directory]')).toBeFocused()
    await expect.poll(() => page.locator('[style*="algorithm-directory-row"]').count()).toBe(0)
    await page.getByRole('button', { name: '上一级', exact: true }).click()
    await expect(preview).toContainText('ROOT')
    await expect(child).toBeFocused()
    if (motion === 'no-preference') {
      await expect.poll(() => page.evaluate(() => JSON.parse(document.documentElement.dataset.transitions!).length)).toBe(2)
      const [, record] = await page.evaluate(() => JSON.parse(document.documentElement.dataset.transitions!))
      expect(record.destination).toBe(record.source)
      expect(record.to - record.from).toBeGreaterThan(30)
    }
    expect(await scroll.evaluate(element => element.scrollTop)).toBe(position)
    await expect.poll(() => page.locator('[style*="algorithm-directory-row"]').count()).toBe(0)
    if (motion !== 'no-preference') {
      expect(await page.evaluate(() => JSON.parse(document.documentElement.dataset.transitions!).length)).toBe(0)
      return
    }
    // 暂停正在播放的过渡，真实点击下一层，验证旧快照不会挡住操作。
    await page.evaluate(() => { document.documentElement.dataset.holdMotion = 'true' })
    await child.click()
    await expect.poll(() => page.evaluate(() => JSON.parse(document.documentElement.dataset.transitions!).length)).toBe(3)
    await page.screenshot({ path: testInfo.outputPath('directory-motion-midpoint.png'), animations: 'allow' })
    await navigation.getByRole('button', { name: 'nested', exact: true }).click()
    await expect(preview).toContainText('NESTED')
    await expect.poll(() => page.evaluate(() => Number(document.documentElement.dataset.skips))).toBeGreaterThan(0)
    await page.getByRole('button', { name: '关闭目录选择器', exact: true }).click()
    await expect(page.getByRole('dialog', { name: '选择算法目录' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '添加算法目录', exact: true })).toBeFocused()
    await expect.poll(() => page.locator('[style*="algorithm-directory-row"]').count()).toBe(0)
  })
}

test('中断初次读取后不触发兜底请求，迟到的结果不能覆盖新目录', async ({ page }) => {
  await setup(page, [algorithmDirectory])
  await expect(page.getByRole('list', { name: '已添加的算法目录' })).toContainText('PREV-VWAP-OPEN')
  let release!: () => void
  const pending = new Promise<void>(resolve => { release = resolve })
  const requests: string[] = []
  const settled: string[] = []
  await page.route('**/api/v1/algorithms/discover**', async route => {
    const path = new URL(route.request().url()).searchParams.get('path') ?? 'DEFAULT'
    requests.push(path)
    if (path === algorithmDirectory) await pending
    await route.fulfill({ json: { path, parent: '/', entries: [], discovery: { files: 1, algorithms: [{ name: path === '/new' ? 'NEW' : 'OLD', function: 'run', file: 'run.py' }], issues: [], can_use: true } } }).catch(() => {})
    settled.push(path)
  })
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  await expect.poll(() => requests).toContain(algorithmDirectory)
  const location = await editDirectoryPath(page)
  await location.fill('/new')
  await location.press('Enter')
  await expect(page.getByRole('region', { name: '当前目录的算法' })).toContainText('NEW')
  release()
  await expect.poll(() => settled).toContain(algorithmDirectory)
  await expect(page.getByRole('button', { name: '编辑目录路径', exact: true })).toBeVisible()
  expect(requests).toEqual([algorithmDirectory, '/new'])
  await expect(page.getByRole('button', { name: '添加此目录', exact: true })).toBeEnabled()
})

test('名称未知的文件先展示入口，点击后显示实际注册算法', async ({ page }, testInfo) => {
  await setup(page)
  let loads = 0
  await page.route('**/api/v1/algorithms/discover**', route => route.fulfill({ json: {
    path: algorithmDirectory, parent: '/', entries: [],
    discovery: { files: 1, algorithms: [{ name: '', function: 'breakeven_maker', file: 'breakeven_maker.py' }, { name: '', function: 'second_entry', file: 'breakeven_maker.py' }], issues: [], can_use: true },
  } }))
  await page.route('**/api/v1/algorithms/load', route => {
    loads++
    expect(route.request().method()).toBe('POST')
    expect(route.request().postDataJSON()).toEqual({ directory: algorithmDirectory, file: 'breakeven_maker.py' })
    if (loads === 1) return route.fulfill({ status: 400, json: { detail: '缺少依赖：example' } })
    return route.fulfill({ json: [{ name: 'BREAKEVEN-MAKER', label: '保本挂单', description: '', default_params: {}, params_schema: {}, channels: ['binance'], slots: null, builtin: false }] })
  })
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  const preview = page.getByRole('region', { name: '当前目录的算法' })
  await expect(preview).toContainText('breakeven_maker.py')
  await expect(preview).toContainText('待加载')
  await expect(page.getByRole('button', { name: '添加此目录', exact: true })).toBeEnabled()
  expect(loads).toBe(0)
  await expect(preview.getByRole('status')).toContainText('算法文件 · 1')
  await expect(preview.getByRole('button', { name: '加载 breakeven_maker.py', exact: true })).toHaveCount(1)
  for (const dark of [false, true]) {
    await page.evaluate(value => document.documentElement.classList.toggle('dark', value), dark)
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 800 })
      expect(await preview.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`file-entry-${dark}-${width}.png`) })
    }
  }
  await page.setViewportSize({ width: 1280, height: 800 })
  await preview.getByRole('button', { name: '加载 breakeven_maker.py', exact: true }).click()
  await expect(preview.getByRole('alert')).toContainText('缺少依赖：example')
  await preview.getByRole('button', { name: '加载 breakeven_maker.py', exact: true }).click()
  await expect(preview).toContainText('BREAKEVEN-MAKER')
  await expect(preview.getByRole('button', { name: '已加载 breakeven_maker.py', exact: true })).toBeDisabled()
  expect(loads).toBe(2)
  await expect(preview).not.toContainText('待加载')
  await page.screenshot({ path: testInfo.outputPath('file-entry-loaded.png') })
})

test('路径输入即时过滤并用 Tab 进入', async ({ page }) => {
  await setup(page)
  const requests: string[] = []
  await page.route('**/api/v1/algorithms/discover**', route => {
    const path = new URL(route.request().url()).searchParams.get('path') ?? '/home/ichika'
    requests.push(path)
    return route.fulfill({ json: { path: path.replace(/\/$/, ''), parent: '/home', entries: path === '/home/ichika' ? [{ name: 'my-algos', path: '/home/ichika/my-algos' }, { name: 'gm-sdk', path: '/home/ichika/gm-sdk' }] : [], discovery: { files: 0, algorithms: [], issues: [], can_use: false } } })
  })
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  const input = await editDirectoryPath(page)
  const navigation = page.getByRole('region', { name: '目录导航' })
  await expect(input).toHaveValue('/home/ichika')
  const count = requests.length
  await input.fill('/home/ichika/my')
  await expect(navigation.getByRole('button', { name: 'my-algos', exact: true })).toBeVisible()
  await expect(navigation.getByRole('button', { name: 'gm-sdk', exact: true })).toHaveCount(0)
  expect(requests.length).toBe(count)
  await input.fill('/home/ichika/zz')
  await expect(navigation).toContainText('没有匹配的目录')
  await input.fill('/home/ichika/my')
  await input.press('ArrowDown')
  await expect(input).toBeFocused()
  await input.press('Tab')
  await expect(input).toHaveValue('/home/ichika/my-algos/')
  await expect.poll(() => requests.at(-1)).toBe('/home/ichika/my-algos/')
  await expect(input).toBeFocused()
})

for (const reduced of [false, true]) {
  test(`筛选补位动效可中断且遵循减少动态效果：${reduced}`, async ({ page }) => {
    await setup(page)
    await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' })
    await page.route('**/api/v1/algorithms/discover**', route => route.fulfill({ json: {
      path: '/home/ichika', parent: '/home', entries: ['aaa', 'my-one', 'bbb', 'my-two'].map(name => ({ name, path: `/home/ichika/${name}` })),
      discovery: { files: 0, algorithms: [], issues: [], can_use: false },
    } }))
    await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
    const input = await editDirectoryPath(page)
    await expect(input).toHaveValue('/home/ichika')
    await page.evaluate(() => {
      const original = Element.prototype.animate
      document.documentElement.dataset.filterAnimations = '0'
      Element.prototype.animate = function (...args) {
        const animation = original.apply(this, args)
        if (this.matches('[data-directory-entry]')) {
          animation.pause()
          document.documentElement.dataset.filterAnimations = String(Number(document.documentElement.dataset.filterAnimations) + 1)
        }
        return animation
      }
    })
    await input.fill('/home/ichika/my')
    const navigation = page.getByRole('region', { name: '目录导航' })
    await expect(navigation.getByRole('button', { name: 'aaa', exact: true })).toHaveCount(0)
    if (!reduced) {
      await expect.poll(() => page.evaluate(() => Number(document.documentElement.dataset.filterAnimations))).toBeGreaterThan(0)
      const frames = await navigation.getByRole('button', { name: 'my-two', exact: true }).evaluate(element => (element.getAnimations()[0].effect as KeyframeEffect).getKeyframes())
      expect(frames[0].transform).not.toBe('translateY(0px)')
    }
    await input.fill('/home/ichika/my-t')
    await expect(input).toBeFocused()
    await expect(navigation.getByRole('button', { name: 'my-one', exact: true })).toHaveCount(0)
    if (reduced) expect(await page.evaluate(() => Number(document.documentElement.dataset.filterAnimations))).toBe(0)
    await input.fill('/home/ichika/')
    if (!reduced) {
      const frames = await navigation.getByRole('button', { name: 'aaa', exact: true }).evaluate(element => (element.getAnimations()[0].effect as KeyframeEffect).getKeyframes())
      expect(Number(frames[0].opacity)).toBe(0)
      await page.emulateMedia({ reducedMotion: 'reduce' })
      await expect.poll(() => page.evaluate(() => document.getAnimations().filter(a => a.playState === 'paused').length)).toBe(0)
    }
    await page.getByRole('button', { name: '关闭目录选择器', exact: true }).click()
    expect(await page.evaluate(() => document.getAnimations().filter(a => a.playState === 'paused').length)).toBe(0)
  })
}

test('地址栏原位切换、Ctrl+L 编辑、失败保留输入与两级 Escape', async ({ page }, testInfo) => {
  await setup(page)
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '选择算法目录', exact: true })
  const edit = dialog.getByRole('button', { name: '编辑目录路径', exact: true })
  await expect(edit).toBeVisible()
  await expect(dialog.getByRole('textbox', { name: '目录路径' })).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: '前往', exact: true })).toHaveCount(0)
  const address = dialog.locator('form')
  const height = await address.evaluate(element => element.getBoundingClientRect().height)
  const shell = address.locator('div').first()
  const bounds = await shell.boundingBox()
  await page.keyboard.press('Control+l')
  const input = dialog.getByRole('textbox', { name: '目录路径' })
  await expect(input).toBeFocused()
  expect(await shell.boundingBox()).toEqual(bounds)
  await input.evaluate(element => { element.dataset.retained = 'yes' })
  await expect(input).toHaveValue(algorithmDirectory)
  expect(await input.evaluate(element => (element as HTMLInputElement).selectionEnd! - (element as HTMLInputElement).selectionStart!)).toBe(algorithmDirectory.length)
  expect(await address.evaluate(element => element.getBoundingClientRect().height)).toBe(height)
  await input.fill('/typed')
  await input.press('Escape')
  await expect(edit).toBeFocused()
  await expect(dialog).toBeVisible()
  expect(await shell.boundingBox()).toEqual(bounds)
  await edit.click()
  await expect(input).toHaveAttribute('data-retained', 'yes')
  await expect(input).toHaveValue(algorithmDirectory)
  await page.route('**/api/v1/algorithms/discover?path=%2Fmissing', route => route.fulfill({ status: 404, json: { detail: '目录不存在' } }))
  await input.fill('/missing')
  await input.press('Enter')
  await expect(dialog.getByText('目录不存在', { exact: true }).filter({ visible: true })).toBeVisible()
  await expect(input).toHaveValue('/missing')
  await input.fill(algorithmDirectory)
  await input.press('Enter')
  await expect(edit).toBeVisible()
  await expect(input).toHaveCount(0)
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 800 })
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`unified-address-${width}.png`) })
  }
  await edit.click()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
})


test('路径输入失焦恢复面包屑、不抢焦点且前往仍可提交', async ({ page }) => {
  await setup(page)
  await page.getByRole('button', { name: '添加算法目录', exact: true }).click()
  const input = await editDirectoryPath(page)
  await input.fill('/unsubmitted')
  const hidden = page.getByRole('button', { name: '显示隐藏目录', exact: true })
  await hidden.click()
  await expect(hidden).toBeFocused()
  await expect(page.getByRole('button', { name: '编辑目录路径', exact: true })).toBeVisible()
  await expect(input).toHaveCount(0)
  await editDirectoryPath(page)
  await expect(input).toHaveValue(algorithmDirectory)
  await input.fill('/new/path')
  await page.getByRole('button', { name: '前往', exact: true }).click()
  await expect(page.getByRole('button', { name: '编辑目录路径', exact: true })).toBeVisible()
  await editDirectoryPath(page)
  await expect(input).toHaveValue('/new/path')
  await input.press('Tab')
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: '编辑目录路径', exact: true })).toBeVisible()
  await expect(input).toHaveCount(0)
})
