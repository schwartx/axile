import { Check } from 'lucide-react'
import { useLayoutEffect, useMemo, useState } from 'react'
import { useLocation } from 'react-router'
import { SectionLabel } from '@/components/ui/Card'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useNavigate } from '@/components/ui/nav'
import { extractFeishuKey } from '@/features/account/feishuUpdate'
import { PortfolioChoiceList } from '@/features/portfolio/PortfolioChoiceList'
import { WeightResult } from '@/features/portfolio/WeightResult'
import { cronToExpr, defaultTimerEditorState, describeCron, resolveCronList, timerEditorError } from '@/features/setup/cron'
import { flowOf } from '@/features/setup/steps'
import { TimerEditor, type TimerEditorState } from '@/features/setup/TimerEditor'
import { WizardHeading, WizardNav, WizardTimerPage } from '@/features/setup/WizardNav'
import { SignalAssetsSetup } from '@/features/signal/SignalAssetsSetup'
import { SignalNotificationSetup, type SignalNotificationMode } from '@/features/signal/SignalNotificationSetup'
import { SignalSources, signalAction } from '@/features/signal/SignalSources'
import { previewSignal, type SignalPreview } from '@/features/signal/signalApi'
import { SIGNAL_NOTIFICATION_EXAMPLE } from '@/features/signal/signalNotificationExample'
import { initialSignalDraft, refreshSignalAccountExample, refreshSignalQuoteExample, SIGNAL_CHANNEL, signalAccountErrors, signalConfig, signalQuoteErrors, type SignalDraft } from '@/features/signal/signalData'
import { createAccount } from '@/lib/api/accounts'
import { useChannelDescriptor } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'

export function SignalSetupPage() {
  const navigate = useNavigate()
  const portfolios = useDomainStore((state) => state.portfolios)
  const refreshAccounts = useDomainStore((state) => state.refreshAccounts)
  const { pathname, search } = useLocation()
  const { index: step, steps } = flowOf(pathname, search)
  const nextStep = steps[step + 1]
  const notificationStep = step === 5
  const [name, setName] = useState('')
  const [portfolioId, setPortfolioId] = useState<number | null>(null)
  const [data, setData] = useState(initialSignalDraft)
  useLayoutEffect(() => {
    const quoteCode = refreshSignalQuoteExample(data.quoteCode)
    const accountCode = refreshSignalAccountExample(data.accountCode)
    if (quoteCode !== data.quoteCode || accountCode !== data.accountCode) {
      setData((draft) => ({
        ...draft,
        quoteCode: refreshSignalQuoteExample(draft.quoteCode),
        accountCode: refreshSignalAccountExample(draft.accountCode),
      }))
    }
  }, [data.quoteCode, data.accountCode])
  const [showQuoteErrors, setShowQuoteErrors] = useState(false)
  const [showAccountErrors, setShowAccountErrors] = useState(false)
  const schedule = useChannelDescriptor(SIGNAL_CHANNEL)?.schedule
  const scheduleKind = schedule?.kind
  const initialTimer = useMemo(() => scheduleKind ? defaultTimerEditorState(scheduleKind) : null, [scheduleKind])
  const [timerDraft, setTimerDraft] = useState<TimerEditorState | null>(null)
  const timer = timerDraft ?? initialTimer
  const [webhook, setWebhook] = useState('')
  const [notificationMode, setNotificationMode] = useState<SignalNotificationMode>('feishu')
  const [confirmedNotification, setConfirmedNotification] = useState<SignalNotificationMode | null>(null)
  const [notification, setNotification] = useState(SIGNAL_NOTIFICATION_EXAMPLE)
  const [error, setError] = useState<Error | null>(null)
  const [preview, setPreview] = useState<SignalPreview | null>(null)
  const [running, setRunning] = useState(false)
  const portfolio = portfolios?.find((item) => item.id === portfolioId)
  const cron = scheduleKind && timer ? cronToExpr(resolveCronList(scheduleKind, timer, schedule.night)) : ''
  const timerBlocked = !scheduleKind || !timer || Boolean(timerEditorError(timer)) || (timer.autoOn && !cron)
  const feishuKey = extractFeishuKey(webhook)
  const missingNotification = { feishu: !feishuKey, function: !notification.trim() }
  const notificationBlocked = missingNotification[notificationMode]
  const confirmedNotificationBlocked = confirmedNotification !== null && missingNotification[confirmedNotification]

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

  const quoteErrors = signalQuoteErrors(data)
  const accountErrors = signalAccountErrors(data)
  const validateQuotes = () => {
    setShowQuoteErrors(true)
    return !Object.values(quoteErrors).some(Boolean)
  }

  const updateDraft = (next: SignalDraft) => {
    if (next.quoteKind !== data.quoteKind) setShowQuoteErrors(false)
    if (next.mode !== data.mode) setShowAccountErrors(false)
    setData(next)
    setPreview(null)
    setError(null)
  }

  const advance = async () => {
    setError(null)
    try {
      if (step === 2 && !validateQuotes()) return
      if (step === 3) {
        setShowAccountErrors(true)
        if (Object.keys(accountErrors).length) return
      }
      if (nextStep) {
        if (notificationStep) {
          if (notificationBlocked) return
          setConfirmedNotification(notificationMode)
        }
        navigate(nextStep.path)
        return
      }
      if (!timer || timerBlocked || confirmedNotificationBlocked) return
      if (portfolioId !== null && !portfolio) return
      const account = await createAccount({
        name: name.trim(),
        market: portfolio?.market ?? '',
        trade_channel: SIGNAL_CHANNEL,
        brokerage: SIGNAL_CHANNEL,
        portfolio_id: portfolioId,
        account_config: signalConfig(data),
        account_control_preset: 'default',
        is_started: timer.autoOn,
        cron_expr: cron,
        algorithm: {},
        weight_precision: 0.01,
        remark: null,
        trade_rules: null,
        forbidden_symbols: null,
        risk_symbols: null,
        write_empty_record: null,
        feishu_key: confirmedNotification === 'feishu' ? feishuKey : null,
        ...(confirmedNotification === 'function' ? { execution_notification_code: notification } : {}),
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
    navigate(steps[step - 1].path)
  }
  const blocked = [
    false,
    !name.trim() || (portfolioId !== null && !portfolio),
    false,
    false,
    timerBlocked,
    notificationBlocked,
    running || timerBlocked || confirmedNotificationBlocked,
  ][step]
  const kicker = `账户设置 · ${step + 1} / ${steps.length}`
  const errorNotice = <ErrorNotice title="调仓信号设置失败" intent="mutation" error={error} />

  if (step === 4) {
    const heading = <WizardHeading
      kicker={kicker}
      title="什么时候自动执行？"
      lead={portfolio ? '按下面的节奏自动计算并生成调仓信号。时间均为北京时间。' : '绑定组合后，才能按设定的节奏自动生成调仓信号。时间均为北京时间。'}
    />
    return <WizardTimerPage footer={<>
      {errorNotice}
      <WizardNav onPrev={previous} onNext={advance} nextDisabled={blocked} />
    </>}>
      {scheduleKind && timer ? <TimerEditor
        tradeChannel={SIGNAL_CHANNEL}
        scheduleKind={scheduleKind}
        nightSchedule={schedule.night}
        value={timer}
        layout="page"
        leading={heading}
        onChange={(next) => setTimerDraft((current) => typeof next === 'function' ? next(current ?? timer) : next)}
      /> : <>
        {heading}
        <p className="mt-[18px] text-[15px] text-ink-2">渠道能力加载中…</p>
      </>}
    </WizardTimerPage>
  }

  const quoteStep = step === 2
  const assetStep = step === 3
  const workspaceStep = quoteStep || assetStep || notificationStep
  const lead = quoteStep ? '选择组合计算使用的行情来源，可在右侧用指定标的测试。'
    : assetStep ? '提供组合计算使用的账户权益、可用资金和币种。实际持仓可选填。'
    : notificationStep ? '选择调仓信号生成后的通知方式。' : null
  const heading = <div className="min-w-0">
    <h1 id="signal-step-title" className="text-[23px] font-[680] tracking-tight">{steps[step].label}</h1>
    {lead && <p className="mt-1.5 max-w-[560px] text-[15px] text-ink-2">{lead}</p>}
  </div>
  const footer = <>
    {errorNotice}
    <WizardNav inline onPrev={previous} onNext={advance} nextDisabled={blocked} nextLabel={nextStep ? '下一步' : '创建账户'}
      secondaryAction={notificationStep && nextStep && <button type="button"
        className="cursor-pointer rounded-[11px] px-3 py-2.5 text-[15px] text-ink-2 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent"
        onClick={() => { setError(null); setConfirmedNotification(null); navigate(nextStep.path) }}>
        跳过通知
      </button>} />
  </>

  return <section aria-labelledby="signal-step-title" className={`grid min-w-0 gap-4 px-8 ${workspaceStep ? 'h-full grid-rows-[minmax(0,1fr)] py-4' : 'py-8'}`}>
    {!workspaceStep && heading}
    <div className={`min-w-0 gap-4 ${workspaceStep ? 'flex min-h-0 flex-col' : 'grid'}`}>
      {step === 1 && <div className="grid gap-2">
        <ConnectionField
          label="账户名称"
          kind="text"
          required
          placeholder="给这个账户起个名字"
          value={name}
          onChange={setName}
        />
        <section className="grid gap-1.5">
          <div className="flex items-center justify-between gap-2">
            <SectionLabel>选择组合（选填）</SectionLabel>
            <button
              type="button"
              aria-pressed={portfolioId === null}
              onClick={() => { setPortfolioId(null); setPreview(null) }}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1 text-[13px] text-ink-2 hover:bg-fill focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              {portfolioId === null && <Check size={14} aria-hidden />}不绑定组合
            </button>
          </div>
          <PortfolioChoiceList
            selectedId={portfolioId}
            onSelect={(next) => { setPortfolioId(next); setPreview(null) }}
          />
        </section>
      </div>}
      {quoteStep && <SignalSources
        only="quote"
        quoteHeading={heading}
        quoteFooter={footer}
        quoteErrors={showQuoteErrors ? quoteErrors : undefined}
        onValidateQuotes={validateQuotes}
        value={data}
        savedQuoteCode={initialSignalDraft.quoteCode}
        onChange={updateDraft}
        identity="signal:setup"
      />}
      {assetStep && <SignalAssetsSetup value={data} errors={accountErrors} showErrors={showAccountErrors}
        name={name.trim()} market={portfolio?.market ?? ''} heading={heading} footer={footer}
        onChange={(patch) => updateDraft({ ...data, ...patch })} onMissingCode={() => setShowAccountErrors(true)} />}
      {notificationStep && <SignalNotificationSetup mode={notificationMode} webhook={webhook} code={notification}
        heading={heading} footer={footer} onModeChange={setNotificationMode} onWebhookChange={setWebhook} onCodeChange={setNotification} />}
      {!nextStep && <>
        <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-2 text-[14px]">
          <dt className="text-ink-3">账户</dt><dd>{name}</dd>
          <dt className="text-ink-3">组合</dt><dd>{portfolio ? `${portfolio.name} · ${portfolio.market}` : '未绑定'}</dd>
          <dt className="text-ink-3">定时任务</dt>
          <dd>{timer?.autoOn && scheduleKind ? describeCron(scheduleKind, cron, schedule.night) ?? cron : '已暂停'}</dd>
          <dt className="text-ink-3">通知</dt>
          <dd>{confirmedNotificationBlocked ? '待填写' : confirmedNotification === 'feishu' ? '飞书' : confirmedNotification === 'function' ? '自定义函数' : '未配置'}</dd>
        </dl>
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            className={signalAction}
            onClick={() => void runPreview()}
            disabled={running || portfolioId === null}
          >
            {running ? '计算中…' : '试算目标数量'}
          </button>
          {portfolioId === null && <span className="text-[13px] text-ink-3">绑定组合后可试算目标数量</span>}
        </div>
        {preview && <WeightResult target={preview.target.targets} mode="quantity" quantityUnit={preview.quantity_unit} />}
      </>}
      {!workspaceStep && footer}
    </div>
  </section>
}
