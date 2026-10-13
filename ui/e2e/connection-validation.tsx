import { useState } from 'react'
import { createRoot } from 'react-dom/client'
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
    <div className="mt-4 max-w-[340px]"><ConnectionValidation descriptor={descriptor} config={config} /></div>
  </main>
}
document.documentElement.classList.add('dark')
createRoot(document.getElementById('root')!).render(<Fixture />)
