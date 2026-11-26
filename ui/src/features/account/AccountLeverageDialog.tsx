import { useState } from 'react'

import { AccountConfigDialog } from '@/features/account/AccountConfigDialog'
import { LeverageInput } from '@/features/account/LeverageInput'
import { WeightPrecisionInput } from '@/features/account/WeightPrecisionInput'
import { leverageError } from '@/features/account/leverage'
import { leverageSettingsPatch } from '@/features/account/leverageSettings'
import { weightPrecisionError } from '@/features/account/weightPrecision'
import type { Account, ChannelCapability } from '@/types/api'

export function AccountLeverageDialog({ accountId, account, channel, onClose, onSaved }: {
  accountId: number
  account: Account
  channel: ChannelCapability
  onClose: () => void
  onSaved: (updated: Account) => void
}) {
  const [baseline] = useState(account)
  const [long, setLong] = useState(String(baseline.long_leverage ?? ''))
  const [short, setShort] = useState(String(baseline.short_leverage ?? ''))
  const [precision, setPrecision] = useState(String(baseline.weight_precision))
  const showShort = channel.ui.show_short_leverage
  const longError = leverageError(long, { ...channel.leverage, allowEmpty: baseline.long_leverage == null })
  const shortError = showShort
    ? leverageError(short, { ...channel.leverage, allowEmpty: baseline.short_leverage == null })
    : null
  const precisionError = weightPrecisionError(precision)
  const patch = leverageSettingsPatch(baseline, {
    long_leverage: long.trim() ? Number(long) : null,
    short_leverage: showShort && short.trim() ? Number(short) : null,
    weight_precision: Number(precision),
  })

  return (
    <AccountConfigDialog
      accountId={accountId}
      accountName={account.name}
      title="杠杆设置"
      patch={longError || shortError || precisionError ? null : patch}
      onClose={onClose}
      onSaved={onSaved}
    >
      <div className="flex flex-wrap gap-x-4 gap-y-3">
        <div className="flex w-full max-w-56 flex-col gap-1">
          <label htmlFor="account-settings-long" className="text-sm text-ink-2">{channel.ui.long_leverage_label}</label>
          <LeverageInput id="account-settings-long" value={long} onChange={setLong} limits={channel.leverage}
            invalid={!!longError} describedBy={longError ? 'account-settings-long-error' : undefined} />
          {longError && <p id="account-settings-long-error" className="text-xs text-warn">{longError}</p>}
        </div>
        {showShort && (
          <div className="flex w-full max-w-56 flex-col gap-1">
            <label htmlFor="account-settings-short" className="text-sm text-ink-2">{channel.ui.short_leverage_label}</label>
            <LeverageInput id="account-settings-short" value={short} onChange={setShort} limits={channel.leverage}
              invalid={!!shortError} describedBy={shortError ? 'account-settings-short-error' : 'account-settings-short-hint'} />
            <p id="account-settings-short-hint" className="text-xs text-ink-3">0 = 不做空</p>
            {shortError && <p id="account-settings-short-error" className="text-xs text-warn">{shortError}</p>}
          </div>
        )}
      </div>
      <div className="w-fit space-y-1">
        <label htmlFor="account-settings-precision" className="text-sm text-ink-2">权重精度</label>
        <WeightPrecisionInput id="account-settings-precision" value={precision} onChange={setPrecision}
          invalid={!!precisionError} error={precisionError} />
      </div>
    </AccountConfigDialog>
  )
}
