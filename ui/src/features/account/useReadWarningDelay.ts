import { useEffect, useState } from 'react'

/** 同一读取身份持续异常五秒才提示；恢复、换身份或隐藏时取消。 */
export function useReadWarningDelay(identity: string, unavailable: boolean, enabled = true) {
  const delayKey = enabled && unavailable ? identity : null
  const [delayedKey, setDelayedKey] = useState<string | null>(null)
  useEffect(() => {
    if (delayKey === null) return
    const timer = window.setTimeout(() => setDelayedKey(delayKey), 5000)
    return () => {
      window.clearTimeout(timer)
      setDelayedKey(null)
    }
  }, [delayKey])
  return delayKey !== null && delayedKey === delayKey
}
