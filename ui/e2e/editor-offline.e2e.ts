import { expect, test, type Page } from '@playwright/test'

const code = 'from axile.server.context import Context\n\ndef calculate_portfolio(context: Context):\n    return {"rb": context.get_price("rb")}\n'
const key = 'portfolio:/portfolios/7/edit'

async function seedDraft(page: Page) {
  await page.evaluate(({ code, key }) => new Promise<void>((resolve, reject) => {
    const open = indexedDB.open('axile-editor', 1)
    open.onsuccess = () => {
      const db = open.result
      const transaction = db.transaction('drafts', 'readwrite')
      transaction.objectStore('drafts').put({ key, kind: 'portfolio', path: '/portfolios/7/edit', title: '测试组合', code, baseline: code, revision: 1, updatedAt: Date.now() })
      transaction.oncomplete = () => { db.close(); resolve() }
      transaction.onerror = () => reject(transaction.error)
    }
    open.onerror = () => reject(open.error)
  }), { code, key })
}

test('editor waits for consent even with the Service Worker, reports both assets, and remembers off', async ({ page, context }) => {
  await page.emulateMedia({ colorScheme: 'dark' })
  const requests: string[] = []
  context.on('request', request => { if (/\.wasm(?:$|[-.])|library[^/]*\.bin/.test(request.url())) requests.push(request.url()) })
  await page.goto('/editor')
  await expect(page.getByText('在线打开组合或通知编辑器后，代码会保存到这里。')).toBeVisible()
  await page.evaluate(async () => { await navigator.serviceWorker.ready })
  await seedDraft(page)
  const url = `/editor?draft=${encodeURIComponent(key)}`
  await page.goto(url)
  const toggle = page.getByRole('switch', { name: '本地代码辅助' })
  const controls = page.getByTestId('editor-wasm-controls')
  const editorBox = (await page.locator('.cm-editor').first().boundingBox())!
  expect((await controls.boundingBox())!.y).toBeGreaterThanOrEqual(editorBox.y + editorBox.height)
  await expect(page.getByRole('tooltip')).toHaveCount(0)
  await controls.hover()
  await expect(page.getByRole('tooltip')).toContainText('下载 9.33 MB 后可启用本地代码辅助')
  await page.screenshot({ path: 'test-results/wasm-editor-bottom-hover.png', fullPage: true })
  await page.mouse.move(0, 0)
  await expect(page.getByRole('tooltip')).toHaveCount(0)
  await page.screenshot({ path: 'test-results/wasm-editor-bottom.png', fullPage: true })
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'disabled')
  expect(requests).toEqual([])
  let release!: () => void
  const waiting = new Promise<void>(resolve => { release = resolve })
  await context.route('**/*library*.bin', async route => { await waiting; await route.continue() })
  try {
    await page.getByRole('button', { name: '下载并开启', exact: true }).click()
    await expect(page.getByRole('progressbar', { name: '组件下载进度' })).toBeVisible()
    await expect(page.getByText(/正在下载组件/)).toContainText('9.33 MB')
    await expect.poll(async () => Number(await page.getByRole('progressbar').getAttribute('value'))).toBeGreaterThan(0)
    await page.screenshot({ path: 'test-results/wasm-editor-downloading.png', fullPage: true })
  } finally { release() }
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'ready', { timeout: 15000 })
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  expect(requests.length).toBe(2)
  const editor = page.locator('.cm-content[contenteditable="true"]')
  await editor.click(); await page.keyboard.press('ControlOrMeta+a'); await page.keyboard.insertText('wrong: int = "kept"\n')
  await expect(page.getByText('not assignable', { exact: false }).first()).toBeVisible()
  await toggle.click()
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'disabled')
  await expect(page.getByText('not assignable', { exact: false })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '格式化', exact: true })).toBeDisabled()
  await editor.click(); await page.keyboard.press('ControlOrMeta+z')
  await expect(editor).toContainText('calculate_portfolio')
  await expect(page.getByRole('link', { name: '本地草稿', exact: true })).toHaveText('已保存到本地')
  await page.reload()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await context.setOffline(true)
  await toggle.click()
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'ready', { timeout: 15000 })
  expect(requests.length).toBe(2)
  await page.screenshot({ path: 'test-results/wasm-editor-enabled.png', fullPage: true })
})

test('cleared runtime cache requires another explicit download while retaining the editor preference', async ({ page }) => {
  await page.goto('/editor')
  await expect(page.getByText('在线打开组合或通知编辑器后，代码会保存到这里。')).toBeVisible()
  await seedDraft(page)
  await page.goto(`/editor?draft=${encodeURIComponent(key)}`)
  await page.getByRole('button', { name: '下载并开启', exact: true }).click()
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'ready', { timeout: 15000 })
  await page.evaluate(() => caches.delete('axile-wasm-v1'))
  const downloads: string[] = []
  page.on('request', request => { if (/\.bin$/.test(request.url())) downloads.push(request.url()) })
  await page.reload()
  await expect(page.getByRole('button', { name: '下载更新并启用' })).toBeVisible()
  await expect(page.getByRole('switch', { name: '本地代码辅助' })).toHaveAttribute('aria-checked', 'true')
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'disabled')
  expect(downloads).toEqual([])
})

test('production assets support offline reload, reopening, diagnostics, formatting and draft recovery', async ({ page, context }) => {
  page.on('pageerror', error => { throw error })
  await page.goto('/editor')
  await expect(page.getByText('在线打开组合或通知编辑器后，代码会保存到这里。')).toBeVisible()
  await seedDraft(page)
  await page.goto(`/editor?draft=${encodeURIComponent(key)}`)
  await page.getByRole('button', { name: '下载并开启', exact: true }).click()
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'ready')
  await expect(page.getByRole('status').filter({ hasText: /^可离线使用 ·/ })).toBeVisible()
  await expect(page.getByRole('link', { name: '本地草稿', exact: true })).toHaveText('已保存到本地')
  await context.setOffline(true)
  const apiRequests: string[] = []
  page.on('request', request => { if (request.url().includes('/api/')) apiRequests.push(request.url()) })
  const sockets: string[] = []
  page.on('websocket', socket => sockets.push(socket.url()))
  await page.goto('/portfolios/7/edit')
  await expect(page.getByRole('heading', { name: '测试组合' })).toBeVisible()
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'ready')
  const editor = page.locator('.cm-content[contenteditable="true"]')
  await editor.click()
  await page.keyboard.press('ControlOrMeta+a')
  await page.keyboard.insertText('wrong: int = "离线😀"\n')
  await expect(page.getByText('not assignable', { exact: false }).first()).toBeVisible()
  await editor.click()
  await page.keyboard.press('ControlOrMeta+a')
  await page.keyboard.insertText('value={"中文":1}\n')
  await page.getByRole('button', { name: '格式化', exact: true }).click()
  await expect(editor).toContainText('value = {"中文": 1}')
  await expect(page.getByRole('link', { name: '本地草稿', exact: true })).toHaveText('已保存到本地')
  await page.reload()
  await expect(editor).toContainText('value = {"中文": 1}')
  const reopened = await context.newPage()
  await reopened.goto(`/editor?draft=${encodeURIComponent(key)}`)
  await expect(reopened.locator('.cm-content[contenteditable="true"]')).toContainText('value = {"中文": 1}')
  await expect(reopened.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'ready')
  expect(apiRequests).toEqual([])
  expect(sockets).toEqual([])
  await reopened.close()
})

test('two open tabs cannot silently replace each other’s local edits', async ({ page, context }) => {
  await page.goto('/editor')
  await expect(page.getByText('在线打开组合或通知编辑器后，代码会保存到这里。')).toBeVisible()
  await seedDraft(page)
  const url = `/editor?draft=${encodeURIComponent(key)}`
  await page.goto(url)
  await expect(page.getByRole('link', { name: '本地草稿', exact: true })).toHaveText('已保存到本地')
  const other = await context.newPage()
  await other.goto(url)
  await expect(other.getByRole('link', { name: '本地草稿', exact: true })).toHaveText('已保存到本地')
  await page.locator('.cm-content[contenteditable="true"]').click()
  await page.keyboard.press('ControlOrMeta+a')
  await page.keyboard.insertText('first = 1\n')
  await expect(page.getByRole('link', { name: '本地草稿', exact: true })).toHaveText('已保存到本地')
  await other.locator('.cm-content[contenteditable="true"]').click()
  await other.keyboard.press('ControlOrMeta+a')
  await other.keyboard.insertText('second = 2\n')
  await expect(other.getByRole('alert')).toContainText('本地草稿已被其他页面修改')
  await expect(other.locator('.cm-content[contenteditable="true"]')).toContainText('second = 2')
  const fresh = await context.newPage()
  await fresh.goto(url)
  await expect(fresh.locator('.cm-content[contenteditable="true"]')).toContainText('first = 1')
})

test('a Worker failure rebuilds analysis without replacing the document or undo history', async ({ page }) => {
  await page.addInitScript(() => {
    const Original = window.Worker
    const workers: Worker[] = []
    Object.assign(window, { editorWorkers: workers })
    window.Worker = class extends Original {
      constructor(url: string | URL, options?: WorkerOptions) { super(url, options); workers.push(this) }
    }
  })
  await page.goto('/editor')
  await expect(page.getByText('在线打开组合或通知编辑器后，代码会保存到这里。')).toBeVisible()
  await seedDraft(page)
  await page.goto(`/editor?draft=${encodeURIComponent(key)}`)
  await page.getByRole('button', { name: '下载并开启', exact: true }).click()
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'ready')
  const editor = page.locator('.cm-content[contenteditable="true"]')
  await editor.click()
  await page.keyboard.press('ControlOrMeta+a')
  await page.keyboard.insertText('wrong: int = "kept"\n')
  await expect(page.getByText('not assignable', { exact: false }).first()).toBeVisible()
  await page.evaluate(() => {
    const worker = (window as unknown as { editorWorkers: Worker[] }).editorWorkers[0]!
    worker.dispatchEvent(new ErrorEvent('error', { message: 'test Worker failure', cancelable: true }))
  })
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'ready')
  await expect(editor).toContainText('wrong: int = "kept"')
  await expect(page.getByText('not assignable', { exact: false }).first()).toBeVisible()
  await editor.click()
  await page.keyboard.press('ControlOrMeta+z')
  await expect(editor).toContainText('calculate_portfolio')
})
