import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Section } from '../src/features/account/editUi'
import { ConnectionValidationAside } from '../src/features/account/connectionValidation/ConnectionValidationAside'
import { useDomainStore } from '../src/stores/domain'
import { ConnectionValidation } from '../src/features/account/connectionValidation/ConnectionValidation'
import type { ChannelCapability } from '../src/types/api'
import '../src/styles/theme.css'

const descriptor = {
  channel: 'ctp', available: true,
  connection_validation: { notice: '', steps: [
    ['trader_connection', '交易前置连接'], ['authenticate', '授权码认证'], ['trader_login', '交易账户登录'],
    ['settlement', '结算确认'], ['instruments', '合约查询（螺纹钢）'], ['market_login', '行情登录'],
    ['query_orders', '订单查询'], ['query_trades', '成交查询'], ['query_positions', '持仓查询'],
    ['query_account', '资金查询'], ['ready', '会话就绪'],
  ].map(([id, label]) => ({ id, label })) },
} as ChannelCapability
export function Fixture() {
  const [config, setConfig] = useState({ investor_id: 'full-account' })
  return <main className="min-h-dvh bg-bg p-4 text-ink-1">
    <button type="button" onClick={() => setConfig({ investor_id: 'changed-account' })}>修改配置</button>
    <button type="button" onClick={() => useDomainStore.setState({ accountsError: new TypeError('Failed to fetch') })}>模拟服务失联</button>
    {new URLSearchParams(window.location.search).has('layout') ? <div className="relative mt-7 xl:grid xl:grid-cols-[minmax(0,1fr)_320px] xl:items-start xl:gap-8">
      <ConnectionValidationAside descriptor={descriptor} config={config}>{(trigger) => <div className="min-w-0 [&>div]:mt-0">
        <Section label="CTP 连接" action={trigger}><label className="h-20 rounded-lg border border-line">投资者号<input aria-label="投资者号" value={config.investor_id} onChange={(event) => setConfig({ investor_id: event.target.value })} /></label></Section>
      </div>}</ConnectionValidationAside>
    </div> : <div className="mt-4 max-w-[340px]"><ConnectionValidation descriptor={descriptor} config={config} /></div>}
  </main>
}
document.documentElement.classList.add('dark')
createRoot(document.getElementById('root')!).render(<Fixture />)
