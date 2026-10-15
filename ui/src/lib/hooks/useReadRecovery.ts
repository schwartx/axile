import { useEffect, useRef } from 'react'
import { subscribeReadRecovery } from '@/lib/readRecovery'

/** 订阅当前视图的只读恢复入口；回调更新不重新订阅，卸载后不再触发。 */
export function useReadRecovery(read: () => void | Promise<unknown>, enabled = true): void {
  const current = useRef(read)
  current.current = read
  useEffect(() => {
    if (enabled) return subscribeReadRecovery(() => current.current())
  }, [enabled])
}
