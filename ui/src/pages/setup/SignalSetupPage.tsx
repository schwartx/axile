import { useState } from 'react'
import { useLocation } from 'react-router'
import { AmountInput } from '@/components/ui/AmountInput'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useNavigate } from '@/components/ui/nav'
import { Toggle } from '@/features/account/editUi'
import { extractFeishuKey } from '@/features/account/feishuUpdate'
import { PortfolioChoiceList } from '@/features/portfolio/PortfolioChoiceList'
import { WeightResult } from '@/features/portfolio/WeightResult'
import { flowOf, SIGNAL_STEPS } from '@/features/setup/steps'
import { WizardNav, WizardPage } from '@/features/setup/WizardNav'
import { SignalCodeEditor, SignalSources, signalAction } from '@/features/signal/SignalSources'
import { previewSignal, type SignalPreview } from '@/features/signal/signalApi'
import { initialSignalDraft, SIGNAL_CHANNEL, signalConfig } from '@/features/signal/signalData'
import { createAccount } from '@/lib/api/accounts'
import { useDomainStore } from '@/stores/domain'

export function SignalSetupPage() {
  const navigate = useNavigate()
  const portfolios = useDomainStore((state) => state.portfolios)
  const refreshAccounts = useDomainStore((state) => state.refreshAccounts)
  const { pathname, search } = useLocation()
  const step = flowOf(pathname, search).index - 1
  const current = SIGNAL_STEPS[step + 1]
  const [name, setName] = useState('')
  const [portfolioId, setPortfolioId] = useState<number | null>(null)
  const [deposit, setDeposit] = useState('')
  const [data, setData] = useState(initialSignalDraft)
  const [enabled, setEnabled] = useState(false)
  const [cron, setCron] = useState('')
  const [webhook, setWebhook] = useState('')
  const [customNotify, setCustomNotify] = useState(false)
  const [notification, setNotification] = useState('')
  const [error, setError] = useState<Error | null>(null)
  const [preview, setPreview] = useState<SignalPreview | null>(null)
  const [running, setRunning] = useState(false)
  const portfolio = portfolios?.find((item) => item.id === portfolioId)

  const runPreview = async () => {
    if (portfolioId === null) return
    setError(null)
    setPreview(null)
    setRunning(true)
    try {
      setPreview(await previewSignal(portfolioId, signalConfig(data)))
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    } finally {
      setRunning(false)
    }
  }

  const advance = async () => {
    setError(null)
    try {
      if (step === 1) signalConfig(data)
      if (step < 3) {
        navigate(SIGNAL_STEPS[step + 2].path)
        return
      }
      if (!portfolio || portfolioId === null) return
      const account = await createAccount({
        name: name.trim(),
        market: portfolio.market,
        trade_channel: SIGNAL_CHANNEL,
        brokerage: SIGNAL_CHANNEL,
        portfolio_id: portfolioId,
        account_config: signalConfig(data),
        account_control_preset: 'default',
        is_started: enabled,
        cron_expr: cron.trim(),
        algorithm: {},
        weight_precision: 0.01,
        remark: null,
        trade_rules: null,
        forbidden_symbols: null,
        risk_symbols: null,
        write_empty_record: null,
        feishu_key: customNotify ? null : extractFeishuKey(webhook) || null,
        ...(customNotify ? { execution_notification_code: notification } : {}),
        ...(deposit.trim() ? {
          initial_deposit: { direction: 'deposit', amount: Number(deposit), occurred_at: new Date().toISOString(), note: '首次入金' },
        } : {}),
      })
      void refreshAccounts()
      navigate(`/accounts/${account.id}`)
    } catch (cause) {
      setError(cause instanceof Error ? cause : new Error(String(cause)))
    }
  }

  const previous = () => {
    setError(null)
    setPreview(null)
    navigate(SIGNAL_STEPS[step].path)
  }
  const blocked = [
    !name.trim() || !portfolio || (deposit.trim() !== '' && !(Number(deposit) > 0 && Number.isFinite(Number(deposit)))),
    false,
    (enabled && !cron.trim()) || (customNotify && !notification.trim()),
    running,
  ][step]

  return <div className="flex h-full min-w-0 flex-col">
    <div className="flex-1 overflow-y-auto">
      <WizardPage kicker={`账户设置 · ${step + 2} / 5`} title={current.label}>
        <div className="grid max-w-[850px] min-w-0 gap-4">
          {step === 0 && <>
            <ConnectionField label="账户名称" kind="text" required value={name} onChange={setName} />
            <label className="grid gap-1 text-[13px] text-ink-2">首次入金（选填）
              <AmountInput ariaLabel="首次入金" value={deposit} unit="" onChange={setDeposit} />
            </label>
            <PortfolioChoiceList selectedId={portfolioId} onSelect={setPortfolioId} />
          </>}
          {step === 1 && <SignalSources value={data} onChange={(next) => { setData(next); setPreview(null) }} identity="signal:setup" />}
          {step === 2 && <>
            <ConnectionField label="定时表达式" kind="text" value={cron} onChange={setCron}
              placeholder="30 15 * * 1-5" help="北京时间。留空时可从账户详情手动计算。" />
            <div className="flex items-center gap-2 text-[13px] text-ink-2">
              <Toggle on={enabled} onClick={() => setEnabled(!enabled)} ariaLabel="创建后启用定时任务" />
              <span>创建后启用定时任务</span>
            </div>
            <div className="flex items-center gap-2 text-[13px] text-ink-2">
              <Toggle on={customNotify} onClick={() => setCustomNotify(!customNotify)} ariaLabel="使用自定义通知函数" />
              <span>使用自定义通知函数</span>
            </div>
            {customNotify
              ? <SignalCodeEditor label="自定义通知函数" identity="signal:setup:notify" code={notification} onChange={setNotification} />
              : <ConnectionField label="飞书 Webhook（选填）" kind="secret" value={webhook} onChange={setWebhook} />}
          </>}
          {step === 3 && <>
            <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-2 text-[14px]">
              <dt className="text-ink-3">账户</dt><dd>{name}</dd>
              <dt className="text-ink-3">组合</dt><dd>{portfolio?.name} · {portfolio?.market}</dd>
              <dt className="text-ink-3">定时任务</dt><dd>{enabled ? cron : '已暂停'}</dd>
            </dl>
            <button type="button" className={`${signalAction} justify-self-start`} onClick={() => void runPreview()} disabled={running}>
              {running ? '计算中…' : '试算目标数量'}
            </button>
            {preview && <WeightResult target={preview.target.targets} mode="quantity" quantityUnit={preview.quantity_unit} />}
          </>}
          <ErrorNotice title="调仓信号设置失败" intent="mutation" error={error} />
        </div>
      </WizardPage>
    </div>
    <WizardNav onPrev={previous} onNext={advance} nextDisabled={blocked} nextLabel={step === 3 ? '创建账户' : '下一步'} />
  </div>
}
