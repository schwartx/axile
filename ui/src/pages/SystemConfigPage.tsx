import { useReadRecovery } from '@/lib/hooks/useReadRecovery'
import { useEffect, useState } from 'react'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { SystemAlertPage } from '@/pages/SystemAlertPage'
import { InitWizard } from '@/features/init/InitWizard'
import { initStatus, initValuesFromStatus, peekInitValues, type InitValues } from '@/lib/api/init'

/** 告警使用独立配置页；高级系统设置沿用需要重启的配置向导。 */
export function SystemConfigPage({ section = 'alert' }: { section?: 'alert' | 'advanced' }) {
  return section === 'alert' ? <SystemAlertPage /> : <AdvancedSystemConfigPage />
}

function AdvancedSystemConfigPage() {
  const [error, setError] = useState<Error | null>(null)
  const [attempt, setAttempt] = useState(0)
  useReadRecovery(() => setAttempt(value => value + 1))
  const [values, setValues] = useState<InitValues | null>(() => peekInitValues())

  useEffect(() => {
    if (values) return
    let alive = true
    setError(null)
    const controller = new AbortController()
    initStatus(controller.signal)
      .then((s) => {
        if (alive) setValues(initValuesFromStatus(s.values))
      })
      .catch((cause) => { if (alive) setError(cause instanceof Error ? cause : new Error(String(cause))) })
    return () => {
      alive = false
      controller.abort()
    }
  }, [values, attempt])

  if (!values) {
    return (
      <div className="grid h-full place-items-center bg-bg text-ink-3">
        <div><ErrorNotice intent="read" title="系统配置读取失败" error={error} onRetry={() => setAttempt(value => value + 1)} />{!error && <p>读取配置中…</p>}</div>
      </div>
    )
  }

  return <InitWizard initial={values} mode="edit" editSection="advanced" />
}
