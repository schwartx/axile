import { useEffect } from 'react'
import { useConnectionAlerts } from '@/stores/connectionAlerts'
import { useLiveConnection } from '@/stores/liveConnection'
import { AccountConnectionDetailModal } from '@/features/account/AccountConnectionDetailModal'

/** 无需停留在账户页也能收到故障；状态仍以共享 SSE 为准。 */
export function AccountConnectionAlerts() {
  const { frames, connected } = useLiveConnection()
  const { accountId, requestId, observe, close } = useConnectionAlerts()
  useEffect(() => {
    if (connected && accountId !== null && !frames[accountId]) close()
    else if (connected) observe(frames)
  }, [frames, connected, accountId, observe, close])
  const detail = accountId === null ? null : frames[accountId]
  return <AccountConnectionDetailModal
    selection={accountId !== null && detail ? { accountId, requestId, detail } : null} onClose={close} />
}
