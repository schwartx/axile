import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { CronSetting } from '../src/features/setup/CronSetting'
import '../src/styles/theme.css'

export function Fixture() {
  const [value, setValue] = useState('0 9 * * 0')
  return <main className="p-6 text-ink-1">
    <CronSetting value={value} onConfirm={setValue} />
    <output aria-label="保存的表达式" className="block">{value}</output>
  </main>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
