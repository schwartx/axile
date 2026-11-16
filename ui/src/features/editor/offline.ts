import { useEffect, useState } from 'react'

let status = '离线资源尚未准备'
const listeners = new Set<(status: string) => void>()
let preparation: Promise<void> | undefined
function publish(value: string) { status = value; listeners.forEach(listener => listener(value)) }

export function useOfflineEditor() {
  const [value, setValue] = useState(status)
  useEffect(() => {
    listeners.add(setValue)
    setValue(status)
    preparation ??= prepareOfflineEditor()
    return () => { listeners.delete(setValue) }
  }, [])
  return value
}

export async function prepareOfflineEditor() {
  if (!('serviceWorker' in navigator)) { publish('离线打开需要 HTTPS 或 localhost，以及支持 Service Worker 的浏览器'); return }
  if (import.meta.env.DEV) { publish('开发模式仅使用本地分析；离线打开请使用生产构建'); return }
  publish('正在准备离线资源…')
  try {
    const registration = await navigator.serviceWorker.register('/editor-sw.js', { scope: '/', updateViaCache: 'none' })
    const check = () => {
      if (registration.waiting) publish('新版离线资源已就绪，关闭页面后生效')
      else if (navigator.serviceWorker.controller) publish('可离线使用 · 清理站点数据会删除草稿与离线资源')
    }
    navigator.serviceWorker.addEventListener('controllerchange', check)
    registration.addEventListener('updatefound', () => {
      const installing = registration.installing
      installing?.addEventListener('statechange', () => {
        if (installing.state === 'redundant') publish('离线资源准备失败，当前代码仍可编辑')
        else check()
      })
    })
    const ready = await navigator.serviceWorker.ready
    // A navigation can commit between activation and its initial clients.claim().
    if (!navigator.serviceWorker.controller) ready.active?.postMessage('claim-editor')
    check()
  } catch (cause) { publish(`离线资源准备失败：${cause instanceof Error ? cause.message : String(cause)}`) }
}
