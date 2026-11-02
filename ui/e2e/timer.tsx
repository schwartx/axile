import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { TimerEditor } from '../src/features/setup/TimerEditor'
import { parseSavedTimer } from '../src/features/setup/cron'
import '../src/styles/theme.css'

export function Fixture() {
  const stock = new URLSearchParams(location.search).get('channel') === 'gm'
  const [value, onChange] = useState(() => parseSavedTimer(stock ? 'cn_stock' : 'cn_futures', '55 14 * * *', null, { count: 2, interval_minutes: 1 }))
  return <main style={{ height: 600, padding: 24 }}>
    <TimerEditor tradeChannel={stock ? 'gm' : 'ctp'} scheduleKind={stock ? 'cn_stock' : 'cn_futures'} value={value} onChange={onChange} layout="page" />
  </main>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
