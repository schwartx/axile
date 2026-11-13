import { useEffect, useId, useState } from 'react'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { TEXT } from '@/features/account/editUi'
import { apiSend } from '@/lib/api/client'

/** 由本机服务检测来源，避免浏览器跨域限制；换地址或关闭时取消旧检测。 */
export function TransferSourceInput({ value, onChange, disabled }: {
  value: string
  onChange: (value: string) => void
  disabled: boolean
}) {
  const helpId = useId()
  const source = value.trim()
  const parsed = URL.canParse(source) ? new URL(source) : null
  const valid = !!parsed && ['http:', 'https:'].includes(parsed.protocol) && !!parsed.hostname
    && !parsed.username && !parsed.password && !parsed.search && !parsed.hash
  const [result, setResult] = useState<{ source: string; message: string; failed?: boolean } | null>(null)
  useEffect(() => {
    if (disabled || !valid) return
    const controller = new AbortController()
    let alive = true
    const timer = setTimeout(() => {
      setResult({ source, message: '正在检测来源端…' })
      void apiSend('POST', '/transfer/check-source', { source_url: source }, controller.signal).then(() => {
        if (alive) setResult({ source, message: '已连接来源端' })
      }).catch(cause => {
        if (alive) setResult({ source, message: cause instanceof Error ? cause.message : String(cause), failed: true })
      })
    }, 500)
    return () => { alive = false; clearTimeout(timer); controller.abort() }
  }, [source, valid, disabled])
  const current = result?.source === source ? result : null
  const message = !source ? '输入地址后自动检测' : !valid ? '请输入有效的 http:// 或 https:// 地址' : current?.message ?? '等待检测…'
  return <div>
    <label className="block text-[14px] text-ink-2">来源端地址
      <input type="url" className={`${TEXT} mt-2`} value={value} disabled={disabled}
        placeholder="http(s)://host:port" aria-describedby={helpId} onChange={event => onChange(event.target.value)} />
    </label>
    <p id={helpId} aria-live="polite" aria-atomic="true" className="mt-2 min-h-5 text-xs">
      <InkRewrite text={message} textClassName={current?.failed || (source && !valid) ? 'text-warn' : 'text-ink-3'} />
    </p>
  </div>
}
