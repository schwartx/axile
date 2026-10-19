import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useViewTransitionState } from 'react-router'
import { Pencil, Play, RefreshCw, Zap } from 'lucide-react'
import { Link } from '@/components/ui/nav'
import { BackLink } from '@/components/ui/BackLink'
import { Chip } from '@/components/ui/Card'
import { Skeleton } from '@/components/ui/Skeleton'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { InkRewrite } from '@/components/ui/InkRewrite'
import { Select } from '@/components/ui/Select'
import { Segmented } from '@/components/ui/Segmented'
import { Tooltip } from '@/components/ui/Tooltip'
import { ConfirmModal, type ConfirmSpec } from '@/components/ui/ConfirmModal'
import type { PythonEditorHandle } from '@/components/ui/PythonFunctionEditor'
import { PythonWorkbench } from '@/components/ui/PythonWorkbench'
import { usePythonDraft, usePythonSave } from '@/components/ui/pythonWorkbenchState'
import { PythonWorkbenchPane } from '@/components/ui/PythonWorkbenchPane'
import { PythonRunPanel } from '@/components/ui/PythonRunPanel'
import { WorkbenchPanel } from '@/components/ui/WorkbenchPanel'
import { channelLabel } from '@/features/dashboard/display'
import { WeightResult } from '@/features/portfolio/WeightResult'
import { SnapshotResult } from '@/features/portfolio/SnapshotResult'
import { BatchExecutionResults } from '@/features/portfolio/BatchExecutionResults'
import type { BatchResult } from '@/features/portfolio/batchExecutionSnapshot'
import { targetSnapshotText } from '@/features/portfolio/targetSnapshotDisplay'
import { targetWeightSummary } from '@/features/portfolio/portfolioCardSummary'
import { useCustomCalcValidation } from '@/features/portfolio/useCustomCalcValidation'
import { portfolioNameVtName } from '@/features/portfolio/viewTransition'
import { usePolling } from '@/lib/hooks/usePolling'
import { useTargetSnapshot } from '@/lib/hooks/useTargetSnapshot'
import {
  getPortfolio,
  getPortfolioTargetSnapshot,
  refreshPortfolioTargetSnapshot,
  updatePortfolio,
  runAndSavePortfolio,
} from '@/lib/api/portfolios'
import { triggerExecute } from '@/lib/api/executions'
import { useDomainStore } from '@/stores/domain'
import { useLiveExecStore } from '@/stores/liveExec'
import { useToastStore } from '@/stores/ui'

const INSPECTOR_WIDTH_KEY = 'axon.portfolioWorkbench.inspectorWidth'

const PANEL_SPLIT_KEY = 'axon.portfolioWorkbench.panelSplit'

function initialPanelSplit() {
  if (typeof window === 'undefined') return 0.5
  const stored = Number(window.localStorage.getItem(PANEL_SPLIT_KEY))
  return Number.isFinite(stored) && stored >= 0.2 && stored <= 0.8 ? stored : 0.5
}

/**
 * 组合工作台（VSCode 式布局）：左列是控制 / 试跑结果 / 跟随账户，
 * 右列是代码 / 问题（问题贴代码底部，共用一条水平 split）；
 * 业务输出与代码诊断各归其位。
 * 保存成功后留在原地（改 → 试跑 → 保存 → 再改的循环不被打断）。
 * 窄视口（<md）退化为单列堆叠，编辑器保底 420px 高、页面恢复滚动。
 */
export function PortfolioEditPage() {
  const { id } = useParams()
  const portfolioId = Number(id)
  const toast = useToastStore((state) => state.toast)
  const portfolio = usePolling(useCallback((signal: AbortSignal) => getPortfolio(portfolioId, signal), [portfolioId]), {
    queryKey: `portfolio:${portfolioId}`,
    intervalMs: 0,
  })
  const accounts = useDomainStore((state) => state.accounts)
  const refreshPortfolios = useDomainStore((state) => state.refreshPortfolios)
  const portfolios = useDomainStore((state) => state.portfolios)
  const pf = portfolio.data
  // 组合列表已在共享 store 里：先用 lite 预填工作台（L1 消闪，标题槽即时挂上共享名），
  // 详情到位后再对齐服务端真值。
  const lite = portfolios?.find((p) => p.id === portfolioId) ?? null
  const head = pf ?? lite
  const [ready, setReady] = useState(false)
  const draftState = usePythonDraft({ name: lite?.name ?? '', code: lite?.custom_calc_py_code ?? '' },
    (a, b) => a.name.trim() === b.name && a.code === b.code)
  const { draft: { name, code }, setDraft, baseline: original, setBaseline: setOriginal } = draftState
  const setName = useCallback((name: string) => setDraft((draft) => ({ ...draft, name })), [setDraft])
  const setCode = useCallback((code: string) => setDraft((draft) => ({ ...draft, code })), [setDraft])
  const saveState = usePythonSave()
  const { saving, error: saveError } = saveState
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null)
  const [batchResults, setBatchResults] = useState<BatchResult[]>([])
  const batchGeneration = useRef(0)
  const batchBusy = useRef(new Set<number>())
  useEffect(() => { setBatchResults([]); batchBusy.current.clear(); return () => { batchGeneration.current += 1 } }, [portfolioId])
  // 「目标」面板默认展开生效 tab：这是组合域的「当前事实」，多数进来是为看它或试跑。
  const [resultOpen, setResultOpen] = useState(true)
  const [targetTab, setTargetTab] = useState<'effective' | 'trial'>('effective')
  const [followersOpen, setFollowersOpen] = useState(true)
  const [panelSplit, setPanelSplit] = useState(initialPanelSplit)
  const [resizingPanels, setResizingPanels] = useState(false)
  const nameEditorRef = useRef<HTMLInputElement>(null)
  const editorRef = useRef<PythonEditorHandle>(null)
  const [editorStatus, setEditorStatus] = useState<HTMLDivElement | null>(null)
  const inspectorRef = useRef<HTMLDivElement>(null)
  const inspectorControlsRef = useRef<HTMLElement>(null)

  // 基线同步：lite 先预填，详情到位后再对齐服务端真值；用户已动过的输入不覆盖，
  // 只把基线对齐，避免 dirty 误判。
  useEffect(() => {
    if (ready) return
    const source = pf ?? lite
    if (!source) return
    const untouched = name === original.name && code === original.code
    setOriginal({ name: source.name, code: source.custom_calc_py_code })
    if (untouched) {
      setName(source.name)
      setCode(source.custom_calc_py_code)
    }
    if (pf) setReady(true)
  }, [pf, lite, ready, name, code, original, setOriginal, setName, setCode])

  const followers = useMemo(
    () => (accounts ?? []).filter((account) => account.portfolio_id === portfolioId),
    [accounts, portfolioId],
  )
  const anyFollowerRunning = useLiveExecStore((state) =>
    followers.some((follower) => state.running.has(follower.account_id)),
  )

  // 生效目标：持久快照（账户下次调仓实际使用的权重），与试跑结果分属两个来源，
  // 由「目标」面板的 生效/试跑 分段显式命名。
  const weights = useTargetSnapshot(
    useCallback((s: AbortSignal) => getPortfolioTargetSnapshot(portfolioId, s), [portfolioId]),
    useCallback(() => refreshPortfolioTargetSnapshot(portfolioId), [portfolioId]),
    `portfolio:${portfolioId}:target-snapshot`,
  )

  // 试跑状态机（与初始化向导共用）：上下文选择、结果、stale、Ctrl+Enter 的回调目标。
  const calc = useCustomCalcValidation(code)

  // 成功只展开返回值（左列）并切到试跑分段，失败只展开问题（右列贴代码）；两列是独立 split pane。
  useEffect(() => {
    if (!calc.editorResult) return
    if (calc.editorResult.valid) {
      setResultOpen(true)
      setTargetTab('trial')
    } else {
      setResultOpen(false)
    }
  }, [calc.editorResult])

  // 组合名 + 市场 chip 共享元素：列表卡 ↔ 本页左栏标题槽（匹配过渡的任一端，列表直达工作台）。
  const tDetail = useViewTransitionState(`/portfolios/${portfolioId}/edit`)

  const notifyFollower = async (accountId: number, name: string) => {
    if (batchBusy.current.has(accountId)) return
    batchBusy.current.add(accountId)
    const version = batchGeneration.current
    const write = (next: BatchResult) => setBatchResults(previous => previous.some(item => item.accountId === accountId)
      ? previous.map(item => item.accountId === accountId ? next : item) : [...previous, next])
    write({ accountId, name, trigger: null, error: null, pending: true })
    try {
      const trigger = await triggerExecute(accountId)
      if (version === batchGeneration.current) write({ accountId, name, trigger, error: null, pending: false })
    } catch (cause) {
      if (version === batchGeneration.current) write({ accountId, name, trigger: null, error: cause instanceof Error ? cause : new Error(String(cause)), pending: false })
    } finally { if (version === batchGeneration.current) batchBusy.current.delete(accountId) }
  }
  const runFanout = () => {
    if (!followers.length || batchBusy.current.size) return
    setConfirm({ title: '全部跟随账户立即执行',
      body: `通知跟随本组合的账户（${followers.map(follower => follower.name).join('、')}）立即按最新目标调仓。`, okText: '通知执行',
      onConfirm: async () => {
        setBatchResults([])
        await Promise.all(followers.map(follower => notifyFollower(follower.account_id, follower.name)))
      },
    })
  }

  const snapshotContextName =
    accounts?.find((account) => account.account_id === weights.data?.context_account_id)?.name
    ?? (weights.data?.context_account_id != null ? `#${weights.data.context_account_id}` : null)
  const snapshotStatusText = weights.data?.calculated_at
    ? `${weights.data.context_account_id == null ? '样例数据' : `账户 ${snapshotContextName}`} · 目标更新于 ${weights.data.calculated_at.slice(11, 16)}`
    : weights.error
      ? '目标不可用'
      : weights.loading
        ? '正在读取目标…'
        : '目标未计算'
  // 生效 tab 时头部状态行改述快照新旧（不是试跑的 pass/fail），重算入口贴着它影响的状态。
  const snapshotStatus = (
    <span className="ml-auto flex items-center gap-1 px-3.5 text-[12.5px]">
      <span
        title={targetSnapshotText(weights.data, snapshotContextName)}
        className="text-ink-3"
      >
        <InkRewrite text={snapshotStatusText} tone="label" />
      </span>
      <Tooltip
        content={
          anyFollowerRunning
            ? '账户正在执行，结束后可重新计算'
            : weights.recalculating
              ? '正在重新计算目标权重'
              : '重新计算当前目标权重'
        }
      >
        <span className="inline-flex flex-none">
          <button
            type="button"
            onClick={() => void weights.recalculate()}
            disabled={anyFollowerRunning || weights.loading || weights.recalculating}
            aria-label={weights.recalculating ? '正在重新计算目标权重' : '重新计算当前目标权重'}
            className="grid h-6 w-6 cursor-pointer place-items-center rounded-md text-ink-3 hover:bg-fill hover:text-ink-1 disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent"
          >
            <RefreshCw size={14} className={weights.recalculating ? 'animate-spin motion-reduce:animate-none' : undefined} />
          </button>
        </span>
      </Tooltip>
    </span>
  )

  const dirty = draftState.dirty
  // 只让当前代码的语法错误阻止保存；继续编辑后，旧试跑结果已 stale，不应误锁。
  const currentSyntaxError =
    !calc.stale && calc.editorResult?.valid === false && calc.editorResult.errorType === 'SyntaxError'
  const blocked =
    !name.trim() || !code.trim() || currentSyntaxError || calc.validating

  // 左列 split：从上顶（控制区底）量指针位置，panelSplit 是返回值占可用高度的份额；
  // 与右列的从底量法互成镜像。
  const panelSplitAt = (clientY: number) => {
    const inspectorBounds = inspectorRef.current?.getBoundingClientRect()
    const controlsBounds = inspectorControlsRef.current?.getBoundingClientRect()
    if (!inspectorBounds || !controlsBounds) return panelSplit
    const available = inspectorBounds.bottom - controlsBounds.bottom - 5
    if (available <= 0) return panelSplit
    const minShare = Math.min(0.45, 120 / available)
    return Math.min(Math.max((clientY - controlsBounds.bottom) / available, minShare), 1 - minShare)
  }

  // 七条轨道始终同构：控制区 / 弹性留白 / 结果标题 / 结果正文 / 分隔条 / 跟随账户标题 / 跟随账户正文。
  // 正文高度由内容驱动：有真内容（权重列表 / 跟随账户）的轨道才 fr 分高，否则 auto 贴
  // 内容一行、剩余空间沉底；弹性留白只在两侧都不分高时 1fr（把面板钉在列底，与双收起的
  // 待遇一致），其余时间 0fr 让位。标题的固定 36px 与正文轨分离，避免 minmax 在触及下限
  // 时切换算法造成高度回弹。
  // 生效 tab 的加载骨架 / 错误提示属占位内容，同样占位分高；空仓与未试跑只有一行文案。
  // 坑（实测）：grid 里所有 fr 系数之和 < 1 时，fr 轨只吸收系数占比的剩余空间，余下的会被
  // auto 轨（Stretch auto Tracks 步）吃掉——所以单独成轨时必须用 1fr，只有双方都分高时才
  // 用 panelSplit / 1-panelSplit（两者恒和为 1）。
  // 生效 / 试跑是同一个结果槽的两个来源。任一来源有实质内容时都固定为分高轨，
  // 避免切 tab 时外层 grid 在 auto ↔ fr 间切换，与正文替换叠加成一次高度回弹。
  const effectiveContentful =
    (weights.loading && weights.data == null) ||
    weights.error != null ||
    weights.recalculateError != null ||
    (weights.data?.calculated_at != null &&
      targetWeightSummary(weights.data.weights).entries.length > 0)
  const trialContentful = Boolean(calc.result?.valid && calc.result?.target)
  const resultContentful = effectiveContentful || trialContentful
  const resultFr = resultOpen && resultContentful
  const followersFr = followersOpen && followers.length > 0
  const inspectorRows = `auto minmax(0, ${resultFr || followersFr ? '0fr' : '1fr'}) 36px ${
    !resultOpen ? 'minmax(0, 0fr)' : resultFr ? `minmax(0, ${followersFr ? panelSplit : 1}fr)` : 'auto'
  } ${resultFr && followersFr ? '5px' : '0px'} 36px ${
    !followersOpen ? 'minmax(0, 0fr)' : followersFr ? `minmax(0, ${resultFr ? 1 - panelSplit : 1}fr)` : 'auto'
  }`

  // 保存后留在原地：toast 确认、基线更新为已保存态，迭代循环不跳出工作台。
  const publish = async () => {
    const saveBlocked = !name.trim() || !code.trim()
    if (saveBlocked || currentSyntaxError || calc.validating || saving || !dirty) return
    await saveState.save(true, async () => {
      await updatePortfolio(portfolioId, { name: name.trim(), custom_calc_py_code: code })
      toast('组合已更新')
      setOriginal({ name: name.trim(), code })
      void refreshPortfolios()
    })
  }

  const runAndPublish = async () => {
    if (saving) return
    const submitted = { name: name.trim(), code }
    const result = await calc.run((draft) => runAndSavePortfolio(portfolioId, {
      name: submitted.name, custom_calc_py_code: draft, account_id: calc.accountId,
    }))
    if (result?.valid) {
      setOriginal(submitted)
      toast('组合与目标快照已保存')
      void refreshPortfolios()
      void weights.reloadSnapshot()
    }
  }

  const restore = () => {
    draftState.restore()
    saveState.clearError()
    editorRef.current?.focus()
  }

  // 有 lite 即可渲染工作台（标题槽即时挂共享名）；全空才骨架，加载失败且无 lite 才报错。
  if (!pf && !lite && !portfolio.error) {
    return (
      <section className="mx-auto flex h-full w-full max-w-[1440px] flex-col">
        <div className="flex min-h-0 flex-1 flex-col gap-6 md:flex-row md:gap-8">
          <div className="w-full md:w-[264px] md:flex-none">
            <div className="flex items-center gap-2">
              <Skeleton className="h-5 w-40" />
              <Skeleton className="h-5 w-14" />
            </div>
            <Skeleton className="mt-6 h-4 w-16" />
            <Skeleton className="mt-2 h-[38px] w-full" />
          </div>
          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            <Skeleton className="h-4 w-24" />
            <Skeleton className="mt-2 min-h-[300px] w-full flex-1" />
            <Skeleton className="mt-3 h-9 w-full" />
          </div>
        </div>
      </section>
    )
  }
  if (portfolio.error && !pf) {
    return (
      <section>
        <ErrorNotice intent="read" contextKey={portfolioId} title="组合加载失败" error={portfolio.error} onRetry={portfolio.refresh} />
      </section>
    )
  }

  return (
    <>
    <PythonWorkbench
      storageKey={INSPECTOR_WIDTH_KEY}
      resizingPanels={resizingPanels}
      saveAction={{ dirty, saving, error: saveError, blocked, onSave: () => void publish(), onRestore: restore,
        blockedReason: currentSyntaxError ? '有语法错误' : !name.trim() || !code.trim() ? '内容不完整' : undefined,
        title: followers.length > 0 ? `保存（⌘/Ctrl+S）· ${followers.length} 个账户将在下次调仓使用新函数` : '保存（⌘/Ctrl+S）',
      }}
      status={<div ref={setEditorStatus} className="mr-2 flex-none" />}
      inspector={(
        <div
          ref={inspectorRef}
          className={`grid min-h-0 content-start transition-[grid-template-rows] duration-200 motion-reduce:transition-none ${
            resizingPanels ? '!transition-none' : ''
          }`}
          style={{ gridTemplateRows: inspectorRows }}
        >
          {/* 左上控制区：身份 → 元信息 → 控制，保存区钉在栏底。 */}
          <aside
            ref={inspectorControlsRef}
            className="flex min-h-0 w-full flex-col overflow-y-auto border-line bg-surface px-3 py-2.5 md:border-r"
          >
          <div className="group/name flex flex-none items-center gap-2">
            <BackLink to="/portfolios" label="返回所有组合" />
            <div
              className="min-w-0 flex-1 cursor-text border-b border-line/70 py-0.5 transition-colors duration-200 hover:border-ink-3 focus-within:border-accent"
              onClick={() => nameEditorRef.current?.focus()}
            >
              <input
                ref={nameEditorRef}
                type="text"
                aria-label="组合名称"
                title="编辑组合名称"
                value={name}
                spellCheck={false}
                className="block w-full min-w-0 bg-transparent text-[15px] font-[640] leading-snug text-ink-1 outline-none"
                style={tDetail ? { viewTransitionName: portfolioNameVtName(portfolioId) } : undefined}
                onChange={(event) => {
                  setName(event.currentTarget.value)
                  saveState.clearError()
                }}
                onKeyDown={(event) => {
                  if (event.key !== 'Enter' || event.nativeEvent.isComposing) return
                  event.preventDefault()
                  event.currentTarget.blur()
                }}
              />
            </div>
            {/* 市场是身份元数据：只读中性胶囊，与列表卡同位（不做编辑入口，依赖缺口是已知问题）。 */}
            {head?.market && (
              <Chip className="max-w-32 truncate">
                {head.market}
              </Chip>
            )}
            {!dirty && (
              <Pencil
                size={13}
                aria-hidden
                className="pointer-events-none flex-none text-ink-3 transition-colors duration-200 group-hover/name:text-ink-2 group-focus-within/name:text-ink-3"
              />
            )}
          </div>
          {/* 描述是身份说明、不可编辑（接口只收名称与代码）：名称下一行小字，不占工作台的注意力预算。 */}
          {head?.description && (
            <p className="mt-1.5 text-[12.5px] leading-5 text-ink-3">{head.description}</p>
          )}

          <div className="flex-none">
            <div className="mt-4 flex flex-col gap-1.5">
                <Select<number | null>
                  ariaLabel="试跑数据来源"
                  value={calc.accountId}
                  onChange={calc.setAccountId}
                  options={calc.contextOptions}
                  className="w-full justify-between px-2.5 py-1.5 text-[13.5px]"
                />
                {/* 来源警示：选真实账户才升琥珀（主动交易有真实委托）。
                    样例来源不再加说明——Select 选项本身已写「不连接真实渠道」，此处零信号。 */}
                {calc.accountId != null && (
                <p className="border-l-2 border-warn pl-2 text-[12.5px] leading-5">
                  <InkRewrite
                    text="真实数据 · 主动交易将产生真实委托"
                    tone="label"
                    textClassName="text-warn"
                  />
                </p>
                )}
                <button
                  type="button"
                  className="inline-flex w-full cursor-pointer items-center justify-center gap-1.5 rounded-[6px] border-0 bg-ink-1 px-3 py-1.5 text-[14px] font-[550] text-surface disabled:cursor-default disabled:opacity-45"
                  onClick={() => void runAndPublish()}
                  disabled={!calc.canRun || saving || !name.trim()}
                >
                  <Play size={14} />
                  <InkRewrite
                    tone="label"
                    text={calc.validating ? '试跑中…' : saving ? '保存中…' : '试跑并保存'}
                    textClassName="text-surface"
                  />
                </button>
            </div>

          </div>

          </aside>

          <div aria-hidden />

          {/* 「目标」面板：一个槽位、两个显式命名的来源——生效（持久快照，事实）/ 试跑（假设）。
              头部状态行随分段切换语义；试跑成功自动切到试跑分段（对应当前的自动展开）。 */}
          <PythonRunPanel
            kind="result"
            title="目标"
            open={resultOpen}
            onToggle={() => setResultOpen((open) => !open)}
            className="border-line border-t md:border-r"
            running={calc.validating}
            result={calc.editorResult}
            stale={targetTab === 'trial' && calc.stale}
            headerExtra={(
              <div className="flex flex-none items-center px-2">
                <Segmented
                  size="sm"
                  value={targetTab}
                  options={[
                    { value: 'effective', label: '生效' },
                    { value: 'trial', label: '试跑' },
                  ]}
                  onChange={setTargetTab}
                />
              </div>
            )}
            statusOverride={targetTab === 'effective' ? snapshotStatus : undefined}
            contentOverride={targetTab === 'effective' ? <SnapshotResult contextKey={portfolioId} weights={weights} /> : undefined}
            resultContent={calc.result?.valid && calc.result.target ? <WeightResult target={calc.result.target} /> : null}
          />

          <div
            role="separator"
            aria-label="调整目标面板与跟随账户的高度"
            aria-orientation="horizontal"
            aria-valuemin={20}
            aria-valuemax={80}
            aria-valuenow={Math.round(panelSplit * 100)}
            tabIndex={resultFr && followersFr ? 0 : -1}
            inert={!(resultFr && followersFr)}
            title="拖动分配面板高度 · 双击平均分配"
            className={`group relative z-10 cursor-row-resize touch-none outline-none md:border-r md:border-line ${
              resultFr && followersFr ? 'block' : 'invisible'
            }`}
            onDoubleClick={() => {
              setPanelSplit(0.5)
              window.localStorage.setItem(PANEL_SPLIT_KEY, '0.5')
            }}
            onPointerDown={(event) => {
              if (!(resultFr && followersFr)) return
              event.preventDefault()
              event.currentTarget.setPointerCapture(event.pointerId)
              setResizingPanels(true)
              setPanelSplit(panelSplitAt(event.clientY))
            }}
            onPointerMove={(event) => {
              if (event.currentTarget.hasPointerCapture(event.pointerId)) setPanelSplit(panelSplitAt(event.clientY))
            }}
            onPointerUp={(event) => {
              const finalSplit = panelSplitAt(event.clientY)
              if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                event.currentTarget.releasePointerCapture(event.pointerId)
              }
              setPanelSplit(finalSplit)
              setResizingPanels(false)
              window.localStorage.setItem(PANEL_SPLIT_KEY, String(finalSplit))
            }}
            onPointerCancel={() => setResizingPanels(false)}
            onKeyDown={(event) => {
              let next = panelSplit
              if (event.key === 'ArrowUp') next -= 0.05
              else if (event.key === 'ArrowDown') next += 0.05
              else if (event.key === 'Home') next = 0.2
              else if (event.key === 'End') next = 0.8
              else return
              event.preventDefault()
              next = Math.min(Math.max(next, 0.2), 0.8)
              setPanelSplit(next)
              window.localStorage.setItem(PANEL_SPLIT_KEY, String(next))
            }}
          >
            <span className="absolute inset-x-0 top-1/2 h-px bg-line transition-colors duration-130 group-hover:bg-accent group-focus:bg-accent" />
          </div>

          {/* 跟随关系是组合的静态元数据；面板外壳与「目标」共用。 */}
          <WorkbenchPanel
            title={`跟随账户 · ${followers.length}`}
            open={followersOpen}
            onToggle={() => setFollowersOpen((open) => !open)}
            className="border-t border-line md:border-r"
            bodyClassName="px-1.5 py-1.5"
            status={followers.length > 0 ? (
                <Tooltip
                  content={
                    anyFollowerRunning
                      ? '账户正在执行，结束后可通知'
                      : '通知全部跟随账户按最新目标立即调仓'
                  }
                >
                  <span className="ml-auto inline-flex flex-none items-center pr-2">
                    <button
                      type="button"
                      onClick={runFanout}
                      disabled={anyFollowerRunning || batchResults.some(result => result.pending)}
                      className="flex cursor-pointer items-center gap-1 rounded-md px-2 py-1 text-[12px] font-semibold text-ink-2 hover:bg-fill hover:text-ink-1 disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent"
                    >
                      <Zap size={12} aria-hidden />
                      立即执行
                    </button>
                  </span>
                </Tooltip>
              ) : undefined}
          >
              <BatchExecutionResults key={portfolioId} portfolioId={portfolioId} results={batchResults} onRetry={notifyFollower} />
              {followers.length > 0 ? (
                <ul className="flex flex-col">
                  {followers.map((account) => (
                    <li key={account.account_id}>
                      <Link
                        to={`/accounts/${account.account_id}`}
                        className="group flex items-center gap-2 rounded-[8px] px-2 py-1.5 hover:bg-fill"
                      >
                        <span className="min-w-0 truncate text-[14px] text-ink-1">{account.name}</span>
                        <span className="flex-none rounded-chip bg-fill px-1.5 py-px text-[11.5px] text-ink-2">
                          {channelLabel(account.trade_channel, account.market)}
                        </span>
                        <span className="ml-auto flex-none text-ink-3 transition-colors group-hover:text-ink-1" aria-hidden>
                          ›
                        </span>
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="px-2 py-1.5 text-[13.5px] text-ink-3">暂无账户跟随该组合。</p>
              )}
          </WorkbenchPanel>
        </div>

      )}
    >
        {/* 右列代码工作台：组合页只提供草稿与试跑状态。 */}
        <PythonWorkbenchPane
          kind="portfolio"
          editorRef={editorRef}
          statusTarget={editorStatus}
          storageKey="axon.portfolioWorkbench.editorSplit"
          title="目标函数"
          dirty={dirty}
          docHref="/docs/custom-calc"
          code={code}
          onChange={(value) => {
            setCode(value)
            saveState.clearError()
          }}
          running={calc.validating}
          stale={calc.stale}
          result={calc.editorResult}
          onRun={() => void calc.run()}
          onResizeChange={setResizingPanels}
        />

    </PythonWorkbench>

      <ConfirmModal spec={confirm} onClose={() => setConfirm(null)} />
    </>
  )
}
