import { useEffect, useState } from 'react'
import { WarningPanel, type WarningPanelAnnouncement } from '@/components/ui/WarningPanel'
import { useDomainStore } from '@/stores/domain'
import { useLiveControl } from '@/stores/liveControl'
import {
  QUOTA_REMINDERS_KEY, QuotaReminderLedger, QuotaWarningController,
  quotaOperationName, quotaStageName, type QuotaWarningEntry,
} from '@/features/account/controlWarnings'

/** 唯一全局入口，持续订阅共享快照，跨标签页串行领取展示资格。 */
export function ControlWarningPanel() {
  const accounts = useDomainStore(state => state.accounts)
  const [entries, setEntries] = useState<QuotaWarningEntry[]>([])
  const [announcement, setAnnouncement] = useState<WarningPanelAnnouncement | null>(null)
  const [controller, setController] = useState<QuotaWarningController | null>(null)
  useEffect(() => {
    const controller = new QuotaWarningController({
      frames: () => useLiveControl.getState().frames,
      visible: () => document.visibilityState === 'visible' && useLiveControl.getState().connected,
      ledger: new QuotaReminderLedger(() => window.localStorage),
      exclusive: async run => {
        let ran = false
        const guardedRun = () => { ran = true; run() }
        try {
          if (navigator.locks) await navigator.locks.request(QUOTA_REMINDERS_KEY, guardedRun)
          else guardedRun()
        } catch {
          if (!ran) guardedRun()
        }
      },
      changed: (next, announced) => {
        setEntries(next)
        if (announced.length) {
          const names = useDomainStore.getState().accounts
          const text = announced.map(item => `${names?.find(account => account.account_id === item.accountId)?.name ?? `账户 #${item.accountId}`}，${quotaOperationName(item.operation)}${quotaStageName(item.stage)}`).join('；')
          setAnnouncement(previous => ({ id: (previous?.id ?? 0) + 1, text }))
        }
      },
    })
    setController(controller)
    const update = () => { void controller.update() }
    const unsubscribe = useLiveControl.subscribe(update)
    const storage = (event: StorageEvent) => { if (event.key === QUOTA_REMINDERS_KEY || event.key == null) update() }
    document.addEventListener('visibilitychange', update)
    window.addEventListener('storage', storage)
    update()
    return () => {
      controller.dispose()
      unsubscribe()
      document.removeEventListener('visibilitychange', update)
      window.removeEventListener('storage', storage)
    }
  }, [])
  return <WarningPanel announcement={announcement} onDismiss={id => controller?.dismiss(id)} items={entries.map(item => ({
    id: item.id,
    title: `${accounts?.find(account => account.account_id === item.accountId)?.name ?? `账户 #${item.accountId}`} · 今日${quotaOperationName(item.operation)}`,
    status: quotaStageName(item.stage),
    threshold: item.stage === 'warning' ? `已达到 ${item.percent}% 预警线` : null,
    used: item.used, limit: item.limit, remaining: item.remaining, stale: item.stale,
    href: `/accounts/${item.accountId}/edit/control`,
  }))} />
}
