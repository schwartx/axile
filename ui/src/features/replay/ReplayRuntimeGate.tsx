import { useEffect, useState, type ReactNode } from 'react'
import { useParams } from 'react-router'
import { useNavigate } from '@/components/ui/nav'
import { ConfirmModal } from '@/components/ui/ConfirmModal'
import { WasmDownloadProgress, downloadSize } from '@/components/ui/WasmDownloadProgress'
import { AccountPageTitle } from '@/features/account/pageHead'
import { useWasmResource } from '@/lib/wasmResources'
import { closeRawDisplay } from './rawDisplay'

export function ReplayRuntimeGate({ children }: { children: ReactNode }) {
  const { id, executionId = '' } = useParams()
  const navigate = useNavigate()
  const raw = useWasmResource('raw'), heat = useWasmResource('analysis')
  const [prompt, setPrompt] = useState(true)
  const back = () => { raw.resource.cancel(); heat.resource.cancel(); void navigate(`/accounts/${id}/executions/${executionId}`, { replace: true }) }
  useEffect(() => {
    if (raw.state.phase === 'cached' && heat.state.phase === 'missing') void heat.resource.download()
  }, [raw.state.phase, heat.state.phase, heat.resource])
  useEffect(() => () => { raw.resource.cancel(); heat.resource.cancel(); closeRawDisplay(executionId) }, [raw.resource, heat.resource, executionId])
  if (raw.state.phase === 'cached' && heat.state.phase === 'cached') return children
  const missing = raw.state.phase === 'missing' || raw.state.phase === 'error'
  return <section>
    <AccountPageTitle accountId={Number(id)} page="执行回放" flip={false} back={{ to: `/accounts/${id}/executions/${executionId}`, label: '返回执行详情' }} />
    <ConfirmModal spec={prompt && missing ? { title: '下载回放组件', body: `回放工作台需要原始数据解码和历史深度组件，合计 ${downloadSize(raw.resource.bytes + heat.resource.bytes)}，是否继续？`, okText: '下载并进入回放', onConfirm: () => { void raw.resource.download() } } : null} onClose={() => setPrompt(false)} onCancel={back} />
    <WasmDownloadProgress state={raw.state} onCancel={back} onRetry={() => { void raw.resource.download() }} />
    <WasmDownloadProgress state={heat.state} onCancel={back} onRetry={() => { void heat.resource.download() }} />
    <button type="button" className="py-3 text-accent" onClick={back}>返回执行详情</button>
  </section>
}
