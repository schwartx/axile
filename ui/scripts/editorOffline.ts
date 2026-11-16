import { createHash } from 'node:crypto'
import type { Plugin } from 'vite'

/** Only static resources are cached; trading API responses and writes always use the network. */
export function editorOffline(): Plugin {
  return {
    name: 'axile-offline-editor',
    generateBundle: { order: 'post', handler(_options, bundle) {
      const assets = Object.keys(bundle).filter(name => !name.endsWith('.map'))
      const version = createHash('sha256').update(assets.map(name => {
        const asset = bundle[name]!
        return name + createHash('sha256').update(asset.type === 'chunk' ? asset.code : asset.source).digest('hex')
      }).join('\n')).digest('hex').slice(0, 20)
      // The UI imports the replay entry points, but their data/WASM are unnecessary for editing.
      const files = ['/', ...assets.filter(name => !/\.wasm(?:$|[-.])|library[^/]*\.bin$|raw\.worker|analysis\.worker/.test(name)).map(name => `/${name}`)]
      this.emitFile({ type: 'asset', fileName: 'editor-sw.js', source: `
const CACHE = 'axile-editor-${version}';
const FILES = ${JSON.stringify(files)};
self.addEventListener('install', event => event.waitUntil((async () => {
  try {
    const cache = await caches.open(CACHE);
    await cache.addAll(FILES);
  } catch (error) { await caches.delete(CACHE); throw error; }
})()));
self.addEventListener('activate', event => event.waitUntil((async () => {
  for (const key of await caches.keys()) if (key.startsWith('axile-editor-') && key !== CACHE) await caches.delete(key);
  await self.clients.claim();
})()));
self.addEventListener('message', event => {
  if (event.data === 'claim-editor') event.waitUntil(self.clients.claim());
});
self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  if (request.mode === 'navigate' && (url.pathname === '/editor' || /^(\\/portfolios\\/[^/]+\\/edit|\\/accounts\\/[^/]+\\/edit\\/notification|\\/settings\\/notification)$/.test(url.pathname))) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      if (url.pathname === '/editor') return (await cache.match('/')) || fetch(request);
      try { return await fetch(request); } catch { return (await cache.match('/')) || Response.error(); }
    })());
  } else if (FILES.includes(url.pathname)) {
    event.respondWith((async () => (await (await caches.open(CACHE)).match(url.pathname)) || fetch(request))());
  }
});
` })
    } },
  }
}
