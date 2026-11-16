import { expect, test } from 'bun:test'
import { assertPythonBaseline, createPythonOperationGate, pythonRunIsStale } from '@/components/ui/pythonWorkbenchState'

test('恢复的函数在服务器版本变化后拒绝覆盖，空配置保持相同口径', () => {
  expect(() => assertPythonBaseline('changed', 'old')).toThrow('服务器函数已更新')
  expect(() => assertPythonBaseline('same', 'same')).not.toThrow()
  expect(() => assertPythonBaseline(null, '')).not.toThrow()
})

test('连续操作只提交一次，完成后允许再次操作', async () => {
  const gate = createPythonOperationGate()
  let release!: () => void
  let requests = 0
  const operation = async () => { requests++; await new Promise<void>((resolve) => { release = resolve }); return 'saved' }
  const first = gate(true, operation)
  expect(await gate(true, operation)).toBeNull()
  expect(requests).toBe(1)
  release()
  expect(await first).toBe('saved')
  expect(await gate(true, async () => 'retry')).toBe('retry')
})

test('禁用操作不发请求，失败后可以重试', async () => {
  const gate = createPythonOperationGate()
  let requests = 0
  expect(await gate(false, async () => requests++)).toBeNull()
  expect(requests).toBe(0)
  await expect(gate(true, async () => { throw new Error('网络失败') })).rejects.toThrow('网络失败')
  expect(await gate(true, async () => 'retry')).toBe('retry')
})

test('结果只对应本次输入，改代码或上下文都过期，恢复输入后仍可用', () => {
  const previous = JSON.stringify(['code', 'account:1'])
  expect(pythonRunIsStale(null, null, previous)).toBe(false)
  expect(pythonRunIsStale({ valid: true }, previous, previous)).toBe(false)
  expect(pythonRunIsStale({ valid: true }, previous, JSON.stringify(['edited', 'account:1']))).toBe(true)
  expect(pythonRunIsStale({ valid: true }, previous, JSON.stringify(['code', 'account:2']))).toBe(true)
})
