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

test('production assets support offline editing, reload and draft recovery', async ({ page, context }) => {
  page.on('pageerror', error => { throw error })
  await page.goto('/editor')
  await expect(page.getByText('在线打开组合或通知编辑器后，代码会保存到这里。')).toBeVisible()
  await seedDraft(page)
  await page.goto(`/editor?draft=${encodeURIComponent(key)}`)
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'disabled')
  await expect(page.getByRole('status').filter({ hasText: /^可离线使用 ·/ })).toBeVisible()
  await expect(page.getByRole('link', { name: '本地草稿', exact: true })).toHaveText('已保存到本地')
  await context.setOffline(true)
  const apiRequests: string[] = []
  page.on('request', request => { if (request.url().includes('/api/')) apiRequests.push(request.url()) })
  const sockets: string[] = []
  page.on('websocket', socket => sockets.push(socket.url()))
  await page.goto('/portfolios/7/edit')
  await expect(page.getByRole('heading', { name: '测试组合' })).toBeVisible()
  await expect(page.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'disabled')
  const editor = page.locator('.cm-content[contenteditable="true"]')
  await editor.click()
  await page.keyboard.press('ControlOrMeta+a')
  await page.keyboard.insertText('value={"中文":1}\n')
  await expect(page.getByRole('button', { name: '格式化', exact: true })).toBeDisabled()
  await expect(page.getByRole('link', { name: '本地草稿', exact: true })).toHaveText('已保存到本地')
  await page.reload()
  await expect(editor).toContainText('value={"中文":1}')
  const reopened = await context.newPage()
  await reopened.goto(`/editor?draft=${encodeURIComponent(key)}`)
  await expect(reopened.locator('.cm-content[contenteditable="true"]')).toContainText('value={"中文":1}')
  await expect(reopened.getByRole('status', { name: '代码分析器' })).toHaveAttribute('data-state', 'disabled')
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
