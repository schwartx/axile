import { CircleHelp } from 'lucide-react'
import { useLayoutEffect, useMemo, useState } from 'react'
import { useLocation } from 'react-router'
import { ConnectionField } from '@/components/ui/ConnectionField'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { useNavigate } from '@/components/ui/nav'
import { Segmented } from '@/components/ui/Segmented'
import { Tooltip } from '@/components/ui/Tooltip'
import { describeLeverage } from '@/features/account/configSummary'
import { extractFeishuKey } from '@/features/account/feishuUpdate'
import { leverageError } from '@/features/account/leverage'
import { NoPortfolioButton, PortfolioChoiceList } from '@/features/portfolio/PortfolioChoiceList'
import { portfolioMarketDescriptor, portfolioMarketOptions } from '@/features/portfolio/portfolioMarkets'
import { defaultTimerEditorState, describeCron, timerEditorError, timerStateToCronExpr } from '@/features/setup/cron'
import { flowOf } from '@/features/setup/steps'
import { TimerEditor, type TimerEditorState } from '@/features/setup/TimerEditor'
import { WizardHeading, WizardNav, WizardPage, WizardTimerPage } from '@/features/setup/WizardNav'
import { SignalAssetsSetup } from '@/features/signal/SignalAssetsSetup'
import { SignalLeverageFields } from '@/features/signal/SignalLeverageFields'
import { SignalNotificationSetup, type SignalNotificationMode } from '@/features/signal/SignalNotificationSetup'
import { SignalSources } from '@/features/signal/SignalSources'
import { SIGNAL_NOTIFICATION_EXAMPLE } from '@/features/signal/signalNotificationExample'
import { initialSignalDraft, refreshSignalAccountExample, SIGNAL_CHANNEL, signalAccountErrors, signalConfig, signalQuoteErrors, signalQuoteOptions, type SignalDraft } from '@/features/signal/signalData'
import { createAccount } from '@/lib/api/accounts'
import { useRemountFade } from '@/lib/viewTransition'
import { useChannelCatalogStore } from '@/stores/channels'
import { useDomainStore } from '@/stores/domain'

export function SignalSetupPage() {
  const navigate = useNavigate()
  const portfolios = useDomainStore((state) => state.portfolios)
  const refreshAccounts = useDomainStore((state) => state.refreshAccounts)
  const catalog = useChannelCatalogStore((state) => state.channels)
  const catalogError = useChannelCatalogStore((state) => state.error)
  const refreshCatalog = useChannelCatalogStore((state) => state.refresh)
  const { pathname, search } = useLocation()
  const { index: step, steps } = flowOf(pathname, search)
  const nextStep = steps[step + 1]
  const notificationStep = step === 5
  const [name, setName] = useState('')
  const [market, setMarket] = useState('')
  const revealPortfolios = useRemountFade(Boolean(market))
  const markets = portfolioMarketOptions(catalog ?? [])
  const descriptor = portfolioMarketDescriptor(catalog ?? [], market)
  const signalDescriptor = catalog?.find((channel) => channel.channel === SIGNAL_CHANNEL)
  const showShort = descriptor?.ui.show_short_leverage ?? false
  const [leverage, setLeverage] = useState({ long: '1', short: '1' })
  const [portfolioId, setPortfolioId] = useState<number | null>(null)
  const [data, setData] = useState(initialSignalDraft)
  useLayoutEffect(() => {
    const accountCode = refreshSignalAccountExample(data.accountCode)
    if (accountCode !== data.accountCode) {
      setData((draft) => ({
        ...draft,
        accountCode: refreshSignalAccountExample(draft.accountCode),
      }))
    }
  }, [data.accountCode])
  const [showQuoteErrors, setShowQuoteErrors] = useState(false)
  const [showAccountErrors, setShowAccountErrors] = useState(false)
  const schedule = descriptor?.schedule
  const scheduleKind = schedule?.kind
  const initialTimer = useMemo(() => scheduleKind ? defaultTimerEditorState(scheduleKind) : null, [scheduleKind])
  const [timerDraft, setTimerDraft] = useState<TimerEditorState | null>(null)
  const timer = timerDraft ?? initialTimer
  const [webhook, setWebhook] = useState('')
  const [notificationMode, setNotificationMode] = useState<SignalNotificationMode>('feishu')
  const [confirmedNotification, setConfirmedNotification] = useState<SignalNotificationMode | null>(null)
  const [notification, setNotification] = useState(SIGNAL_NOTIFICATION_EXAMPLE)
  const [error, setError] = useState<Error | null>(null)
  const portfolio = portfolios?.find((item) => item.id === portfolioId && item.market === market)
  const accountBlocked = !name.trim() || !descriptor || !signalDescriptor
    || leverageError(leverage.long, signalDescriptor.leverage) !== null
    || (showShort && leverageError(leverage.short, signalDescriptor.leverage) !== null)
    || (portfolioId !== null && !portfolio)
  const cron = scheduleKind && timer ? timerStateToCronExpr(scheduleKind, timer, schedule.night) : ''
  const gmSessions = descriptor?.channel === 'gm' ? timer?.gm_sessions : null
  const timerBlocked = !scheduleKind || !timer || Boolean(timerEditorError(timer)) || (timer.autoOn && !cron)
  const feishuKey = extractFeishuKey(webhook)
  const missingNotification = { feishu: !feishuKey, function: !notification.trim(), off: false }
  const notificationBlocked = missingNotification[notificationMode]
  const confirmedNotificationBlocked = confirmedNotification !== null && missingNotification[confirmedNotification]

  const chooseMarket = (nextMarket: string) => {
    if (nextMarket === market) return
    const next = portfolioMarketDescriptor(catalog ?? [], nextMarket)
    if (!next?.market) return
    setMarket(nextMarket)
    setPortfolioId(null)
    setData({
      ...initialSignalDraft, currency: next.currency,
      quoteKind: signalQuoteOptions(next.market, catalog ?? [])[0]!.value,
    })
    setTimerDraft(null)
    setShowQuoteErrors(false)
    setShowAccountErrors(false)
    setError(null)
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
      if (accountBlocked || !timer || timerBlocked || confirmedNotificationBlocked) return
      const account = await createAccount({
        name: name.trim(),
        market,
        trade_channel: SIGNAL_CHANNEL,
        brokerage: SIGNAL_CHANNEL,
        portfolio_id: portfolioId,
        account_config: signalConfig(data),
        account_control_preset: 'default',
        is_started: timer.autoOn,
        cron_expr: cron,
        ...(gmSessions ? { channel_options: { gm: gmSessions } } : {}),
        algorithm: {},
        long_leverage: Number(leverage.long),
        short_leverage: showShort ? Number(leverage.short) : 0,
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
    navigate(steps[step - 1].path)
  }
  const blocked = [
    false,
    accountBlocked,
    !descriptor,
    false,
    timerBlocked,
    notificationBlocked,
    accountBlocked || timerBlocked || confirmedNotificationBlocked,
  ][step]
  const kicker = `账户设置 · ${step + 1} / ${steps.length}`
  const errorNotice = <ErrorNotice title="调仓信号设置失败" intent="mutation" error={error} />

  if (step === 1) {
    return <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
        <WizardPage kicker={kicker} title={steps[step].label}>
          <div className="grid max-w-[720px] items-start gap-5 xl:max-w-none xl:grid-cols-[minmax(0,1fr)_minmax(0,1.618fr)] xl:gap-x-16">
            <div className="grid min-w-0 gap-5">
              <ConnectionField label="账户名称" kind="text" required placeholder="给这个账户起个名字"
                value={name} onChange={setName} />
              <section className="grid gap-2">
                <div className="flex items-center gap-1">
                  <h2 className="text-[14px] font-medium text-ink-2">市场</h2>
                  <Tooltip content="切换市场会重置已选组合、行情、资金与持仓和定时设置。" arrow>
                    <button type="button" aria-label="切换市场说明" onClick={(event) => event.currentTarget.focus()}
                      className="inline-flex size-6 cursor-help items-center justify-center rounded text-ink-3 hover:text-ink-2 focus-visible:outline-accent">
                      <CircleHelp size={14} aria-hidden />
                    </button>
                  </Tooltip>
                </div>
                {markets.length > 0 && <Segmented radio size="sm" className="justify-self-start [&>button]:min-h-7 [&>button]:text-[15px]" ariaLabel="市场"
                  value={market} onChange={chooseMarket}
                  options={markets.toSorted((a, b) => Number(b.label === 'A股') - Number(a.label === 'A股'))
                    .map(({ label }) => ({ value: label, label }))} />}
                <ErrorNotice intent="read" title="市场目录加载失败" error={catalogError} onRetry={refreshCatalog} />
                {!catalog && !catalogError && <p className="text-[13px] text-ink-3">加载市场…</p>}
              </section>
              {signalDescriptor && descriptor && <SignalLeverageFields
                limits={signalDescriptor.leverage} ui={descriptor.ui} value={leverage} onChange={setLeverage} />}
              {error && errorNotice}
            </div>
            <section aria-labelledby="signal-portfolio-heading" className="grid min-w-0 gap-2 border-t border-line pt-4 xl:border-t-0 xl:pt-0">
              <div className="flex min-h-7 items-center justify-between gap-2">
                <h2 id="signal-portfolio-heading" className="text-[14px] font-medium text-ink-2">选择组合 <span className="text-ink-3">（选填）</span></h2>
                {descriptor && <NoPortfolioButton selected={portfolioId == null} onSelect={() => setPortfolioId(null)} />}
              </div>
              {descriptor ? <div className={revealPortfolios ? 'motion-safe:animate-[panel-fade-in_150ms_ease-out]' : undefined}>
                <PortfolioChoiceList key={market} market={market} selectedId={portfolioId}
                  emptyMessage="该市场暂无组合，可以继续。"
                  onSelect={setPortfolioId} />
              </div> : <p className="text-[13px] text-ink-3">先选择市场，再查看同市场的组合。</p>}
            </section>
          </div>
        </WizardPage>
      </div>
      <WizardNav onPrev={previous} onNext={advance} nextDisabled={blocked} />
    </div>
  }

  if (!nextStep) {
    return <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <WizardPage kicker={kicker} title={steps[step].label}>
          <div className="wrap-break-word rounded-2xl bg-ok-soft px-7 py-6 text-[20px] leading-loose">
            账户 <b className="font-[680]">{name.trim() || '（未命名）'}</b>，
            {market && <>市场 <b className="font-[680]">{market}</b>，</>}
            {portfolio
              ? <>已绑定组合 <b className="font-[680]">{portfolio.name}</b>。</>
              : '暂不绑定组合。'}
          </div>
          <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs text-ink-3">
            <dt>杠杆</dt>
            <dd>{describeLeverage(Number(leverage.long), Number(leverage.short), showShort)}</dd>
            <dt>定时任务</dt>
            <dd>{timer?.autoOn && scheduleKind ? describeCron(scheduleKind, cron, schedule.night, gmSessions) ?? cron : '已暂停'}</dd>
            <dt>通知</dt>
            <dd>{confirmedNotificationBlocked ? '待填写' : confirmedNotification === 'feishu' ? '飞书' : confirmedNotification === 'function' ? '自定义函数' : '未配置'}</dd>
          </dl>
          {errorNotice}
        </WizardPage>
      </div>
      <WizardNav onPrev={previous} onNext={advance} nextDisabled={blocked} nextLabel="创建账户" />
    </div>
  }

  if (step === 4) {
    const heading = <WizardHeading
      kicker={kicker}
      title="什么时候自动执行？"
      lead={portfolio ? '按下面的节奏自动执行。时间均为北京时间。' : '绑定组合后，才能按设定的节奏自动执行。时间均为北京时间。'}
    />
    return <WizardTimerPage footer={<>
      {errorNotice}
      <WizardNav onPrev={previous} onNext={advance} nextDisabled={blocked} />
    </>}>
      {descriptor && scheduleKind && timer ? <TimerEditor
        tradeChannel={SIGNAL_CHANNEL}
        scheduleChannel={descriptor.channel}
        scheduleKind={scheduleKind}
        nightSchedule={schedule.night}
        value={timer}
        layout="page"
        leading={heading}
        onChange={(next) => setTimerDraft((current) => typeof next === 'function' ? next(current ?? timer) : next)}
      /> : <>
        {heading}
        <p className="mt-[18px] text-[15px] text-ink-2">请先在「账户与组合」选择市场。</p>
      </>}
    </WizardTimerPage>
  }

  const quoteStep = step === 2
  const assetStep = step === 3
  const workspaceStep = (quoteStep && Boolean(descriptor)) || assetStep || notificationStep
  const lead = quoteStep ? '选择组合计算使用的行情来源，可在右侧用指定标的测试。'
    : assetStep ? '提供组合计算使用的账户权益、可用资金和币种。实际持仓可选填。'
    : notificationStep ? '选择调仓信号的通知方式。' : null
  const heading = <div className="min-w-0">
    <h1 id="signal-step-title" className="text-[23px] font-[680] tracking-tight">{steps[step].label}</h1>
    {lead && <p className="mt-1.5 max-w-[560px] text-[15px] text-ink-2">{lead}</p>}
  </div>
  const footer = <>
    {errorNotice}
    <WizardNav inline onPrev={previous} onNext={advance} nextDisabled={blocked}
      secondaryAction={notificationStep && <button type="button"
        className="cursor-pointer rounded-[11px] px-3 py-2.5 text-[15px] text-ink-2 hover:bg-fill hover:text-ink-1 focus-visible:outline-2 focus-visible:outline-accent"
        onClick={() => { setError(null); setConfirmedNotification(null); navigate(nextStep.path) }}>
        跳过通知
      </button>} />
  </>

  return <section aria-labelledby="signal-step-title" className={`grid min-w-0 gap-4 px-8 ${workspaceStep ? 'h-full grid-rows-[minmax(0,1fr)] py-4' : 'py-8'}`}>
    {!workspaceStep && heading}
    <div className={`min-w-0 gap-4 ${workspaceStep ? 'flex min-h-0 flex-col' : 'grid'}`}>
      {quoteStep && !descriptor && <p className="text-[15px] text-ink-2">请先在「账户与组合」选择市场。</p>}
      {quoteStep && descriptor && <SignalSources
        market={descriptor.market ?? ''}
        exampleSymbols={descriptor.portfolio?.example_symbols}
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
        name={name.trim()} market={market} heading={heading} footer={footer}
        onChange={(patch) => updateDraft({ ...data, ...patch })} onMissingCode={() => setShowAccountErrors(true)} />}
      {notificationStep && <SignalNotificationSetup mode={notificationMode} webhook={webhook} code={notification}
        name={name} market={market} heading={heading} footer={footer}
        onModeChange={setNotificationMode} onWebhookChange={setWebhook} onCodeChange={setNotification} />}
      {!workspaceStep && footer}
    </div>
  </section>
}
