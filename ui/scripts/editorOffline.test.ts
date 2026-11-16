import { expect, test } from 'bun:test'
import { runInNewContext } from 'node:vm'
import { editorOffline } from './editorOffline'

test('failed cache installation removes the partial generation and preserves the previous version', async () => {
  let source = ''
  const plugin = editorOffline()
  const generate = plugin.generateBundle as { handler: Function }
  generate.handler.call({ emitFile: (asset: { source: string }) => { source = asset.source } }, {}, {
    'index.html': { type: 'asset', source: '<html />' },
    'assets/python.worker.js': { type: 'chunk', code: 'worker' },
  })
  const keys = new Set(['axile-editor-previous'])
  const handlers = new Map<string, (event: { waitUntil: (promise: Promise<void>) => void }) => void>()
  runInNewContext(source, {
    self: { addEventListener: (type: string, handler: (event: unknown) => void) => handlers.set(type, handler) },
    caches: {
      open: async (key: string) => { keys.add(key); return { addAll: async () => { throw new Error('resource download failed') } } },
      delete: async (key: string) => keys.delete(key),
    },
  })
  let installation: Promise<void> = Promise.resolve()
  handlers.get('install')!({ waitUntil: promise => { installation = promise } })
  await expect(installation).rejects.toThrow('resource download failed')
  expect([...keys]).toEqual(['axile-editor-previous'])
})
