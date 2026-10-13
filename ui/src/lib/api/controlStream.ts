import { useEffect } from 'react'
import { useLiveControl, type ControlEnvelope } from '@/stores/liveControl'

/** 应用级唯一连接；隐藏页停止接收，恢复后重新取得权威全量。 */
export function useLiveControlSync() {
  useEffect(() => {
    let stream: EventSource | null = null
    const sync = () => {
      stream?.close()
      stream = null
      useLiveControl.getState().disconnect()
      if (document.hidden) return
      const current = new EventSource('/api/v1/account/control/stream')
      stream = current
      const receive = (event: Event) => {
        if (stream !== current) return
        try { useLiveControl.getState().apply(JSON.parse((event as MessageEvent).data) as ControlEnvelope) }
        catch { useLiveControl.getState().disconnect() }
      }
      current.addEventListener('snapshot', receive)
      current.addEventListener('update', receive)
      current.onerror = () => { if (stream === current) useLiveControl.getState().disconnect() }
    }
    sync()
    document.addEventListener('visibilitychange', sync)
    const timer = window.setInterval(() => useLiveControl.getState().expire(Date.now()), 1000)
    return () => { stream?.close(); document.removeEventListener('visibilitychange', sync); window.clearInterval(timer) }
  }, [])
}
