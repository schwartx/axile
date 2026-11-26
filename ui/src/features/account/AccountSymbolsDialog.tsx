import { useState } from 'react'

import { SymbolTagInput } from '@/components/ui/SymbolTagInput'
import { AccountConfigDialog } from '@/features/account/AccountConfigDialog'
import type { AccountUpdatePayload } from '@/lib/api/accounts'
import type { Account } from '@/types/api'

function sameList(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((symbol, index) => symbol === b[index])
}

export function AccountSymbolsDialog({ accountId, account, onClose, onSaved }: {
  accountId: number
  account: Account
  onClose: () => void
  onSaved: (updated: Account) => void
}) {
  const [baseline] = useState(() => ({
    forbidden: account.forbidden_symbols ?? [],
    risk: account.risk_symbols ?? [],
  }))
  const [forbidden, setForbidden] = useState(baseline.forbidden)
  const [risk, setRisk] = useState(baseline.risk)
  const patch: AccountUpdatePayload = {}
  if (!sameList(forbidden, baseline.forbidden)) patch.forbidden_symbols = forbidden.length ? forbidden : null
  if (!sameList(risk, baseline.risk)) patch.risk_symbols = risk.length ? risk : null

  return (
    <AccountConfigDialog
      accountId={accountId}
      accountName={account.name}
      title="品种控制"
      patch={patch}
      onClose={onClose}
      onSaved={onSaved}
    >
      <div className="space-y-1.5">
        <label htmlFor="account-forbidden-symbols" className="text-sm text-ink-2">
          禁投 <span className="ml-1 text-xs text-ink-3">永不建仓</span>
        </label>
        <SymbolTagInput
          id="account-forbidden-symbols"
          value={forbidden}
          warn
          placeholder="输入禁投品种…"
          describedBy="account-symbol-entry-hint"
          onChange={setForbidden}
          otherGroup={{
            label: '风险品种',
            value: risk,
            onMove: (symbols) => {
              setForbidden([...forbidden, ...symbols.filter((symbol) => !forbidden.includes(symbol))])
              setRisk(risk.filter((symbol) => !symbols.includes(symbol)))
            },
          }}
        />
      </div>
      <div className="space-y-1.5">
        <label htmlFor="account-risk-symbols" className="text-sm text-ink-2">
          风险品种 <span className="ml-1 text-xs text-ink-3">仅减不加</span>
        </label>
        <SymbolTagInput
          id="account-risk-symbols"
          value={risk}
          placeholder="输入风险品种…"
          describedBy="account-symbol-entry-hint"
          onChange={setRisk}
          otherGroup={{
            label: '禁投',
            value: forbidden,
            onMove: (symbols) => {
              setForbidden(forbidden.filter((symbol) => !symbols.includes(symbol)))
              setRisk([...risk, ...symbols.filter((symbol) => !risk.includes(symbol))])
            },
          }}
        />
      </div>
      <p id="account-symbol-entry-hint" className="text-xs text-ink-3">按 Enter、Tab、逗号或空格确认，支持批量粘贴</p>
    </AccountConfigDialog>
  )
}
