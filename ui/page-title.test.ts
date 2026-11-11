import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { browserTitle } from './src/lib/browserTitle'

const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8')
const script = html.match(/<script>([\s\S]*?)<\/script>/)![1]

test.each([
  ['http://localhost:8000/', 'axile(本地:8000)'],
  ['http://127.0.0.1:1420/portfolios/3/edit', 'axile(本地:1420)'],
  ['http://[::1]:8000/', 'axile(本地:8000)'],
  ['http://localhost/', 'axile(本地)'],
  ['http://192.168.1.10:8000/', 'axile(192.168.1.10:8000)'],
  ['https://axile.example.com/', 'axile(axile.example.com)'],
  ['http://localhost.example.com:8000/', 'axile(localhost.example.com:8000)'],
  ['http://[2001:db8::1]:8000/', 'axile([2001:db8::1]:8000)'],
])('%s → %s', (url, title) => {
  const document = { title: 'axile', documentElement: { classList: { toggle() {} } } }
  runInNewContext(script, { location: new URL(url), document })
  expect(document.title).toBe(title)
  expect(browserTitle('', new URL(url))).toBe(title)
  expect(browserTitle('   ', new URL(url))).toBe(title)
})

test('自定义标题完整替换默认标题，保留中文与普通文本', () => {
  const location = new URL('http://localhost:8001/accounts/5')
  expect(browserTitle('  实盘 · axile  ', location)).toBe('实盘 · axile')
  expect(browserTitle('<测试> & "axile"', location)).toBe('<测试> & "axile"')
})
