import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'

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
})
