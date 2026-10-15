import { FolderOpen } from 'lucide-react'
import { useState } from 'react'
import { Link } from '@/components/ui/nav'
import { BrandWordmark } from '@/components/brand/BrandWordmark'
import { ThemeToggle } from '@/components/ThemeToggle'
import { Toast } from '@/components/Toast'
import { ConfirmModal, type ConfirmSpec } from '@/components/ui/ConfirmModal'
import { DirectoryPicker } from '@/components/ui/DirectoryPicker'
import { OverflowText } from '@/components/ui/OverflowText'
import { usePythonDraft, usePythonRun, usePythonSave } from '@/components/ui/pythonWorkbenchState'
import { PythonFunctionEditor } from '@/components/ui/PythonFunctionEditor'
import { Select } from '@/components/ui/Select'
import { SettingsSaveBar } from '@/components/ui/SettingsSaveBar'
import { StringTagInput } from '@/components/ui/StringTagInput'
import {
  appendUniqueStrings,
  normalizeStringList,
} from '@/components/ui/stringList'
import { ErrorNotice } from '@/components/ui/ErrorNotice'
import { Row, Section, TEXT } from '@/features/account/editUi'
import { SystemAlertFields } from '@/features/system/SystemAlertFields'
import { extractFeishuKey } from '@/features/account/feishuUpdate'
import { advancedConfigChanges } from '@/features/init/advancedConfig'
import { WizardPage } from '@/features/setup/WizardNav'
import { ApiError } from '@/lib/api/client'
import {
  initStatus,
  initSavePayload,
  saveExecutionAlert,
  saveInit,
  testDb,
  testFeishu,
  testSystemNotificationFunction,
  type InitValues,
  type SystemNotificationTestResult,
} from '@/lib/api/init'
import { useToastStore } from '@/stores/ui'

/** 向导运行模式：首启初始化 vs 已配置后从主页进入的系统配置。 */
export type WizardMode = 'init' | 'edit'

const inputCls =
  'w-full rounded-[11px] border border-ink-3/30 bg-surface px-3.5 py-3 text-[16px] outline-none focus:border-accent'
const labelCls = 'mb-1.5 mt-4 block text-[14px] text-ink-2 first:mt-0'

const DEFAULT_DB_URI = 'sqlite+aiosqlite:///./axile.db'

/** 随模式切换的文案；`init`=首启初始化，`edit`=已配置后的系统配置。 */
interface Copy {
  /** 头部与左栏的向导名。 */
  brand: string
  /** 各步 kicker 前缀（如「初始化 · 1 / 3」）。 */
  kicker: string
  /** 第 3 步标题。 */
  confirmTitle: string
  /** 第 3 步引导语。 */
  confirmLead: string
  /** 末步主按钮文案。 */
  saveLabel: string
  /** 保存进行中的按钮文案。 */
  savingLabel: string
  /** 保存成功后的 toast。 */
  savedToast: string
}

const COPY: Record<WizardMode, Copy> = {
  init: {
    brand: '初始化',
    kicker: '初始化',
    confirmTitle: '确认并启动',
    confirmLead: '确认无误后保存；axile 会写入配置并自动重启进入正常模式。',
    saveLabel: '保存并启动',
    savingLabel: '启动中…',
    savedToast: '配置已保存，axile 即将退出…',
  },
  edit: {
    brand: '系统配置',
    kicker: '系统配置',
    confirmTitle: '确认并保存',
    confirmLead: '确认无误后保存。',
    saveLabel: '保存并重启',
    savingLabel: '重启中…',
    savedToast: '配置已保存，axile 正在重启…',
  },
}

// 可选告警集成独立成步；数据库/环境/日志等有可用默认值，收进确认页高级选项。
const INIT_STEP_LABELS = ['执行告警'] as const
const EDIT_STEP_LABELS = ['执行告警'] as const

/** 表单草稿；`algorithm_modules` / `algorithm_directories` 以每行一项的文本承载，保存时切分为数组。 */
interface Draft {
  sqlalchemy_database_uri: string
  exe_err_feishu_key: string
  system_execution_notification_mode: 'default' | 'function'
  system_execution_notification_code: string
  environment: string
  app_log_dir: string
  axile_log_rotation: string
  algorithm_modules: string
  algorithm_directories: string
}

/** 把后端配置归一成可直接比较的表单草稿。 */
function draftFromInitial(initial: InitValues, isEdit = false): Draft {
  return {
    sqlalchemy_database_uri: initial.sqlalchemy_database_uri || (isEdit ? '' : DEFAULT_DB_URI),
    exe_err_feishu_key: initial.exe_err_feishu_key ?? '',
    system_execution_notification_mode: initial.system_execution_notification_mode ?? 'default',
    system_execution_notification_code: initial.system_execution_notification_code ?? '',
    environment: initial.environment || 'local',
    app_log_dir: initial.app_log_dir || './logs',
    axile_log_rotation: initial.axile_log_rotation || '1 day',
    algorithm_modules: (initial.algorithm_modules ?? []).join('\n'),
    algorithm_directories: (initial.algorithm_directories ?? []).join('\n'),
  }
}

function splitLines(value: string): string[] {
  return normalizeStringList(value.split('\n'))
}

function advancedValues(draft: Draft, initial: InitValues): InitValues {
  return {
    ...initial,
    sqlalchemy_database_uri: draft.sqlalchemy_database_uri,
    exe_err_feishu_key: draft.exe_err_feishu_key,
    system_execution_notification_mode: draft.system_execution_notification_mode,
    system_execution_notification_code: draft.system_execution_notification_code,
    environment: draft.environment,
    app_log_dir: draft.app_log_dir,
    axile_log_rotation: draft.axile_log_rotation,
    algorithm_modules: splitLines(draft.algorithm_modules),
    algorithm_directories: splitLines(draft.algorithm_directories),
  }
}

type TestState = { ok: boolean; message: string } | 'busy' | null

function errText(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e)
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * 「测试连接」按钮 + 结果徽标。`idleLabel` 覆盖空闲文案，`disabled` 用于「未填写即不可测」。
 *
 * 结果色走 accent（蓝，✓）/ warn（琥珀，✗）——成败不占红绿（红绿留给涨跌）。
 */
function TestRow({
  state,
  onTest,
  idleLabel = '测试连接',
  disabled = false,
}: {
  state: TestState
  onTest: () => void
  idleLabel?: string
  disabled?: boolean
}) {
  return (
    <div className="mt-4 flex items-center gap-3">
      <button
        className="cursor-pointer rounded-[11px] border border-line bg-surface px-4 py-2.5 text-[15px] text-ink-2 disabled:opacity-45"
        onClick={onTest}
        disabled={disabled || state === 'busy'}
      >
        {state === 'busy' ? '测试中…' : idleLabel}
      </button>
      {state && state !== 'busy' && (
        <span
          className={`text-[14px] ${state.ok ? 'text-accent' : 'text-warn'}`}
        >
          {state.ok ? '✓ ' : '✗ '}
          {state.message}
        </span>
      )}
    </div>
  )
}

/**
 * 左侧步骤栏。
 *
 * 初始化向导逐级解锁，仅「已完成 / 当前」可点击回跳，已完成项打绿勾。
 */
function Rail({
  step,
  steps,
  title,
  onJump,
}: {
  step: number
  steps: readonly string[]
  title: string
  onJump: (i: number) => void
}) {
  return (
    <aside className="flex w-full flex-none overflow-x-auto border-b border-line bg-surface px-3 py-2 sm:block sm:w-[248px] sm:overflow-y-auto sm:border-b-0 sm:border-r sm:px-[18px] sm:py-7">
      <div className="hidden px-2.5 pb-3.5 text-xs font-semibold tracking-wide text-ink-3 sm:block">
        {title}
      </div>
      {steps.map((label, i) => {
        const done = i < step
        const cur = i === step
        const clickable = i <= step
        return (
          <button
            key={label}
            onClick={() => clickable && onJump(i)}
            className={`flex w-auto min-w-fit flex-none items-center gap-2 rounded-[10px] p-2.5 text-left text-[14px] sm:w-full sm:gap-3 sm:text-[15px] ${
              cur ? 'bg-accent-soft font-semibold text-ink-1' : 'text-ink-2'
            } ${clickable ? 'cursor-pointer' : 'cursor-default'}`}
          >
            <span
              className={`grid h-6 w-6 flex-none place-items-center rounded-full border text-xs ${
                done
                  ? 'border-ok bg-ok text-white'
                  : cur
                    ? 'border-accent text-accent'
                    : 'border-border-strong bg-surface text-ink-3'
              }`}
            >
              {done ? '✓' : i + 1}
            </span>
            {label}
          </button>
        )
      })}
    </aside>
  )
}

/**
 * 配置向导（整屏）。
 *
 * 两种模式共用一套表单，但**导航范式不同**：
 * - `init`=首启初始化（由 `AppRoot` 在未配置时整屏渲染）：线性向导，逐级「下一步」推进，Rail 仅回跳已过步骤。
 * - `edit`=已配置后从齿轮进入的系统配置（由 `SystemConfigPage` 承载）：**设置页**，
 *   页内分段可任意切换；执行告警热保存，高级系统设置保存后重启。
 *
 * 采集数据库地址与可选外部集成配置，保存后后端写入 config.toml 并自重启；
 * 期间轮询 `/init/status`，就绪后刷新页面进入正常应用。编辑态保存前先弹确认（重启会中断执行）。
 */
export function InitWizard({
  initial,
  mode = 'init',
  editSection = 'alert',
}: {
  initial: InitValues
  /** 运行模式，默认首启初始化。 */
  mode?: WizardMode
  /** 已配置后的独立设置页；首启向导忽略此项。 */
  editSection?: 'alert' | 'advanced'
}) {
  const isEdit = mode === 'edit'
  const copy = COPY[mode]
  const steps = [
    ...(isEdit ? EDIT_STEP_LABELS : INIT_STEP_LABELS),
    copy.confirmTitle,
  ]

  const toast = useToastStore((s) => s.toast)
  const [step, setStep] = useState(0)
  const saveState = usePythonSave()
  const { saving, error: saveError } = saveState
  const [dbTest, setDbTest] = useState<TestState>(null)
  const [clearAlertKey, setClearAlertKey] = useState(false)
  const [feishuTest, setFeishuTest] = useState<TestState>(null)
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null)
  const [directoryPickerTarget, setDirectoryPickerTarget] = useState<
    'log' | 'algorithm' | null
  >(null)
  const draftState = usePythonDraft(draftFromInitial(initial, isEdit), (a, b) =>
    a.exe_err_feishu_key === b.exe_err_feishu_key && a.system_execution_notification_mode === b.system_execution_notification_mode && a.system_execution_notification_code === b.system_execution_notification_code)
  const { draft, setDraft, baseline: savedAlert } = draftState
  const initialDraft = draftFromInitial(initial, isEdit)
  const currentAdvancedValues = advancedValues(draft, initial)
  const advancedChanges = advancedConfigChanges(initial, currentAdvancedValues)
  const alertDirty = draftState.dirty || clearAlertKey

  const set = (patch: Partial<Draft>) => {
    saveState.clearError()
    if (patch.exe_err_feishu_key !== undefined) setFeishuTest(null)
    setDraft((d) => ({ ...d, ...patch }))
  }

  const runDbTest = async () => {
    setDbTest('busy')
    try {
      setDbTest(await testDb(draft.sqlalchemy_database_uri))
    } catch (e) {
      setDbTest({ ok: false, message: errText(e) })
    }
  }

  const runFeishuTest = async () => {
    setFeishuTest('busy')
    try {
      setFeishuTest(await testFeishu(clearAlertKey ? '' : extractFeishuKey(draft.exe_err_feishu_key) || (isEdit ? null : '')))
    } catch (e) {
      setFeishuTest({ ok: false, message: errText(e) })
    }
  }

  const functionTrial = usePythonRun<SystemNotificationTestResult>({
    code: draft.system_execution_notification_code,
    contextKey: JSON.stringify([draft.exe_err_feishu_key, clearAlertKey, draft.system_execution_notification_mode]),
    enabled: !saving && feishuTest !== 'busy',
    execute: (code) => testSystemNotificationFunction(code, clearAlertKey ? '' : extractFeishuKey(draft.exe_err_feishu_key) || (isEdit ? null : '')),
    failed: (cause) => ({ ok: false, message: errText(cause) }),
    toEditorResult: (result) => ({ valid: result.ok, errorMessage: result.ok ? null : result.message, errorLine: result.error_line }),
  })
  const restoreAlert = () => { setClearAlertKey(false); set({
    exe_err_feishu_key: savedAlert.exe_err_feishu_key,
    system_execution_notification_mode: savedAlert.system_execution_notification_mode,
    system_execution_notification_code: savedAlert.system_execution_notification_code,
  }) }

  /** 保存后轮询就绪状态，容忍重启期间的请求失败，就绪或超时后刷新。 */
  const waitReadyAndReload = async () => {
    const deadline = Date.now() + 90_000
    while (Date.now() < deadline) {
      await sleep(1500)
      try {
        const s = await initStatus()
        if (s.configured) break
      } catch {
        // 后端正在重启，继续轮询。
      }
    }
    window.location.reload()
  }

  const doSave = () => saveState.save(!functionTrial.running && feishuTest !== 'busy', async () => {
    if (isEdit && editSection === 'alert') {
      const result = await saveExecutionAlert(clearAlertKey ? '' : extractFeishuKey(draft.exe_err_feishu_key) || null, draft.system_execution_notification_mode, draft.system_execution_notification_code)
      draftState.setBaseline(draft)
      setClearAlertKey(false)
      toast(result.message)
      return
    }
    await saveInit(initSavePayload({
      ...draft,
      algorithm_modules: splitLines(draft.algorithm_modules),
      algorithm_directories: splitLines(draft.algorithm_directories),
    }, isEdit))
    toast(copy.savedToast)
    await waitReadyAndReload()
  })

  /** 执行告警直接热保存；高级设置保存前确认重启；首启态直接保存。 */
  const onSave = () => {
    if (isEdit && editSection === 'advanced') {
      setConfirm({
        title: '保存并重启服务',
        body: '保存后 axile 会写入配置并重启服务以生效，正在进行中的执行会被中断。确认继续?',
        okText: '保存并重启',
        danger: true,
        onConfirm: () => void doSave(),
      })
    } else {
      void doSave()
    }
  }

  const confirmStep = steps.length - 1
  const alertStep = 0
  const nextDisabled =
    step === confirmStep && !isEdit && !draft.sqlalchemy_database_uri.trim()
  const saveDisabled = saving || (!isEdit && !draft.sqlalchemy_database_uri.trim())

  const resetAdvancedDraft = () => {
    setDraft(initialDraft)
    setDbTest(null)
    saveState.clearError()
  }

  // edit 态是设置页而非向导：kicker 去掉「n / N」序号，只留分节标签。
  const kickerOf = (n: number) =>
    isEdit ? copy.kicker : `${copy.kicker} · ${n} / ${steps.length}`

  const onNext = () => {
    if (step < steps.length - 1) setStep(step + 1)
    else onSave()
  }

  return (
    <div className={`flex flex-col ${isEdit ? 'h-full' : 'h-screen'}`}>
      {!isEdit && (
        <header className="flex h-14 flex-none items-center gap-3.5 border-b border-line bg-surface px-6">
          <Link to="/" aria-label="axile 首页" className="text-ink-1">
            <BrandWordmark size={16.5} />
          </Link>
          <span className="text-[15px] text-ink-2">· {copy.brand}</span>
          <span className="ml-auto" />
          <ThemeToggle />
        </header>
      )}

      <div className="flex flex-1 flex-col overflow-hidden sm:flex-row">
        {!isEdit && (
          <Rail step={step} steps={steps} title={copy.brand} onJump={setStep} />
        )}
        <div className="relative flex flex-1 flex-col overflow-hidden">
          <div
            className={`flex-1 overflow-y-auto [scrollbar-gutter:stable] ${isEdit && editSection === 'advanced' ? 'pb-24' : ''}`}
          >
            {step === alertStep && (!isEdit || editSection === 'alert') && (
              <WizardPage
                kicker={kickerOf(1)}
                title="系统告警（选填）"
                lead="账户执行异常或总超时时发送系统告警。默认模式使用飞书卡片，自定义模式运行通知函数；测试只验证当前草稿，不会保存配置。"
              >
                <div className="max-w-[760px]">
                  <SystemAlertFields
                    draft={{ mode: draft.system_execution_notification_mode, code: draft.system_execution_notification_code, keyInput: draft.exe_err_feishu_key, clearKey: clearAlertKey }}
                    configured={Boolean(initial.exe_err_feishu_configured)}
                    disabled={saving || functionTrial.running || feishuTest === 'busy'}
                    onChange={(patch) => {
                      if (patch.clearKey !== undefined) { setClearAlertKey(patch.clearKey); setFeishuTest(null) }
                      set({ ...(patch.mode !== undefined ? { system_execution_notification_mode: patch.mode } : {}), ...(patch.keyInput !== undefined ? { exe_err_feishu_key: patch.keyInput } : {}) })
                    }}
                    keyActions={<button type="button" className="cursor-pointer rounded-[9px] border border-line px-4 py-2 text-[14px] disabled:opacity-45" onClick={() => void runFeishuTest()} disabled={clearAlertKey || (!draft.exe_err_feishu_key.trim() && !initial.exe_err_feishu_configured) || saving || functionTrial.running || feishuTest === 'busy'}>{feishuTest === 'busy' ? '测试中…' : '测试推送'}</button>}
                    custom={<div className="mt-3">
                      <p className="mb-2 text-[13px] text-ink-3">同步或异步 notify(context) 接收系统执行异常；试跑不会保存配置。</p>
                      <PythonFunctionEditor kind="system_notification" code={draft.system_execution_notification_code} onChange={(code) => set({ system_execution_notification_code: code })} running={functionTrial.running} runDisabled={saving || feishuTest === 'busy'} result={functionTrial.editorResult} resultContent={functionTrial.result ? <p className="text-[14px] text-ink-2">{functionTrial.result.message}</p> : undefined} stale={functionTrial.stale} onRun={() => void functionTrial.run()} saveAction={isEdit ? { dirty: alertDirty, saving, error: saveError, disabled: functionTrial.running || feishuTest === 'busy', onSave, onRestore: restoreAlert } : undefined} workbenchTitle="系统告警函数" docHref="/docs/system-notification" height="320px" />
                    </div>}
                  />
                  {feishuTest && feishuTest !== 'busy' && <p className={`mt-3 text-[13px] ${feishuTest.ok ? 'text-ink-2' : 'text-warn'}`} role="status">{feishuTest.message}</p>}
                  {isEdit && draft.system_execution_notification_mode === 'default' && <button type="button" className="mt-4 cursor-pointer rounded-[9px] bg-ink-1 px-5 py-2 text-surface disabled:opacity-45" onClick={onSave} disabled={!alertDirty || saving || functionTrial.running || feishuTest === 'busy'}>{saving ? '保存中…' : '保存'}</button>}
                  <ErrorNotice intent="mutation" title="保存执行告警失败" error={saveError} variant="mutation" onRetry={doSave} />
                </div>
              </WizardPage>
            )}

            {step === confirmStep && !isEdit && (
              <WizardPage
                kicker={kickerOf(steps.length)}
                title={copy.confirmTitle}
                lead={copy.confirmLead}
              >
                <div className="max-w-[560px]">
                  <dl className="rounded-[14px] border border-line bg-surface px-4 py-2 text-[15px]">
                    {[
                      [
                        '执行告警',
                        draft.system_execution_notification_mode === 'function' ? '自定义函数' : draft.exe_err_feishu_key ? '已配置飞书推送' : '（未配置 · 不推送）',
                      ],
                    ].map(([k, v]) => (
                      <div
                        key={k}
                        className="flex justify-between gap-4 border-b border-line py-2.5 last:border-0"
                      >
                        <dt className="text-ink-3">{k}</dt>
                        <dd className="min-w-0">
                          <OverflowText className="text-ink-1" text={v} />
                        </dd>
                      </div>
                    ))}
                  </dl>

                  <details className="mt-4 rounded-[14px] border border-line bg-surface px-4 py-3">
                    <summary className="cursor-pointer text-[15px] text-ink-2">
                      高级选项（一般保持默认）
                    </summary>
                    <label className={labelCls}>数据库地址</label>
                    <input
                      className={inputCls}
                      placeholder={isEdit ? '已配置 · 留空保持不变' : DEFAULT_DB_URI}
                      value={draft.sqlalchemy_database_uri}
                      onChange={(e) =>
                        set({ sqlalchemy_database_uri: e.target.value })
                      }
                    />
                    <TestRow state={dbTest} onTest={runDbTest} />
                    <label className={labelCls}>运行环境</label>
                    <Select<string>
                      ariaLabel="运行环境"
                      className="w-full justify-between px-3 py-2 text-[15px]"
                      value={draft.environment}
                      onChange={(v) => set({ environment: v })}
                      options={[
                        { value: 'local', label: '本地开发（local）' },
                        { value: 'staging', label: '预发布（staging）' },
                        { value: 'production', label: '生产（production）' },
                      ]}
                    />
                    <p className="mt-1.5 text-[13px] leading-relaxed text-ink-3">
                      仅影响日志详细程度：「本地开发」打印较详细日志（INFO
                      及以上），「预发布 / 生产」更安静（仅警告及以上）。
                      <span className="text-ink-2">
                        不改变交易行为，与实盘 / 模拟无关。
                      </span>
                    </p>
                    <label className={labelCls}>日志目录</label>
                    <input
                      className={inputCls}
                      value={draft.app_log_dir}
                      onChange={(e) => set({ app_log_dir: e.target.value })}
                    />
                    <label className={labelCls}>日志滚动</label>
                    <input
                      className={inputCls}
                      value={draft.axile_log_rotation}
                      onChange={(e) =>
                        set({ axile_log_rotation: e.target.value })
                      }
                    />
                    <label className={labelCls}>用户算法目录（每行一个）</label>
                    <textarea
                      className={`${inputCls} min-h-[72px] font-mono`}
                      value={draft.algorithm_directories}
                      onChange={(e) =>
                        set({ algorithm_directories: e.target.value })
                      }
                      placeholder="./my_algorithms"
                    />
                    <p className="mt-1.5 text-[13px] leading-relaxed text-ink-3">
                      指向存放自定义算法的目录，启动时扫描其中的{' '}
                      <span className="num">.py</span> 并加载注册的算法；
                      保存后服务重启生效，随后在账户「怎么交易」里可选。
                    </p>
                    <label className={labelCls}>
                      算法模块（每行一个 · 进阶）
                    </label>
                    <textarea
                      className={`${inputCls} min-h-[72px] font-mono`}
                      value={draft.algorithm_modules}
                      onChange={(e) =>
                        set({ algorithm_modules: e.target.value })
                      }
                      placeholder="package.module"
                    />
                  </details>
                </div>
              </WizardPage>
            )}

            {isEdit && editSection === 'advanced' && (
              <div className="mx-auto max-w-[1728px] px-5 pt-8 pb-6 sm:px-12">
                <div className="text-[23px] font-[680] tracking-tight">
                  高级
                </div>
                <div className="mt-3 border-l-2 border-warn/60 bg-warn-tint/50 py-2 pl-3 pr-2 text-[14px] text-ink-2">
                  保存后将重启服务，并中断正在进行中的执行。
                </div>

                <Section label="存储">
                  <Row label="数据库地址" hint="持久化" top span>
                    <div className="flex items-center gap-2">
                      <input
                        className={`${TEXT} min-w-0 flex-1 font-mono`}
                        placeholder={isEdit ? '已配置 · 留空保持不变' : DEFAULT_DB_URI}
                        value={draft.sqlalchemy_database_uri}
                        onChange={(event) => {
                          setDbTest(null)
                          set({ sqlalchemy_database_uri: event.target.value })
                        }}
                      />
                      <button
                        type="button"
                        className="flex-none cursor-pointer rounded-[9px] border border-line bg-surface px-4 py-2 text-[15px] text-ink-2 transition-[border-color] hover:border-ink-3/40 disabled:opacity-45"
                        onClick={runDbTest}
                        disabled={
                          !draft.sqlalchemy_database_uri.trim() ||
                          dbTest === 'busy'
                        }
                      >
                        {dbTest === 'busy' ? '测试中…' : '测试连接'}
                      </button>
                    </div>
                    <div
                      className="mt-1.5 min-h-4 text-[13px]"
                      aria-live="polite"
                    >
                      {dbTest && dbTest !== 'busy' ? (
                        <span
                          className={dbTest.ok ? 'text-accent' : 'text-warn'}
                        >
                          {dbTest.ok ? '✓ ' : '✗ '}
                          {dbTest.message}
                        </span>
                      ) : (
                        <span className="text-ink-3">
                          服务数据的持久化地址；更换前请确认目标数据库已准备完成。
                        </span>
                      )}
                    </div>
                  </Row>
                </Section>

                <Section label="运行与日志">
                  <Row label="运行环境" hint="日志级别" top>
                    <Select<string>
                      ariaLabel="运行环境"
                      className="w-full justify-between px-3 py-2 text-[15px]"
                      value={draft.environment}
                      onChange={(value) => set({ environment: value })}
                      options={[
                        { value: 'local', label: '本地开发（local）' },
                        { value: 'staging', label: '预发布（staging）' },
                        { value: 'production', label: '生产（production）' },
                      ]}
                    />
                    <p className="mt-1.5 text-[12px] leading-5 text-ink-3">
                      {draft.environment === 'local'
                        ? '打印 INFO 及以上详细日志。'
                        : '仅记录警告及以上日志。'}
                      不改变交易行为。
                    </p>
                  </Row>
                  <Row label="日志滚动" hint="轮换规则" top>
                    <input
                      className={`${TEXT} font-mono`}
                      value={draft.axile_log_rotation}
                      onChange={(event) =>
                        set({ axile_log_rotation: event.target.value })
                      }
                      placeholder="1 day"
                    />
                    <p className="mt-1.5 text-[12px] leading-5 text-ink-3">
                      例如 1 day、100 MB 或 00:00。
                    </p>
                  </Row>
                  <Row label="日志目录" hint="写入位置" top span>
                    <div className="flex items-center gap-2">
                      <input
                        className={`${TEXT} min-w-0 flex-1 font-mono`}
                        value={draft.app_log_dir}
                        onChange={(event) =>
                          set({ app_log_dir: event.target.value })
                        }
                      />
                      <button
                        type="button"
                        className="inline-flex flex-none cursor-pointer items-center gap-1.5 rounded-[9px] border border-line bg-surface px-4 py-2 text-[15px] text-ink-2 transition-[border-color] hover:border-ink-3/40"
                        onClick={() => setDirectoryPickerTarget('log')}
                      >
                        <FolderOpen size={15} />
                        选择目录
                      </button>
                    </div>
                    <p className="mt-1.5 text-[12px] leading-5 text-ink-3">
                      服务运行日志的写入目录，可直接输入或浏览服务所在机器。
                    </p>
                  </Row>
                </Section>

                <Section label="算法扩展">
                  <Row label="用户算法目录" hint="扫描 .py" top span>
                    <StringTagInput
                      id="advanced-algorithm-directories"
                      value={splitLines(draft.algorithm_directories)}
                      mode="directory"
                      placeholder="输入目录路径…"
                      help="Enter 确认；启动时扫描目录中的 .py 文件并加载注册算法。"
                      onChange={(values) =>
                        set({ algorithm_directories: values.join('\n') })
                      }
                      action={
                        <button
                          type="button"
                          className="inline-flex flex-none cursor-pointer items-center gap-1.5 rounded-[9px] border border-line bg-surface px-3 py-1.5 text-[13px] text-ink-2"
                          onMouseDown={(event) => event.preventDefault()}
                          onClick={() => setDirectoryPickerTarget('algorithm')}
                        >
                          <FolderOpen size={14} />
                          浏览目录
                        </button>
                      }
                    />
                  </Row>
                  <Row label="算法模块" hint="进阶" top span>
                    <StringTagInput
                      id="advanced-algorithm-modules"
                      value={splitLines(draft.algorithm_modules)}
                      mode="module"
                      placeholder="package.module"
                      help="Enter、Tab、逗号或空格确认；按 Python 模块路径加载。"
                      onChange={(values) =>
                        set({ algorithm_modules: values.join('\n') })
                      }
                    />
                  </Row>
                </Section>
              </div>
            )}
          </div>

          {isEdit && editSection === 'advanced' && (
            <SettingsSaveBar
              changes={advancedChanges}
              blocked={saveDisabled && !saving}
              saving={saving}
              onCancel={resetAdvancedDraft}
              onSave={onSave}
              error={saveError}
            />
          )}

          {!isEdit && (
            <>
              <div className="border-t border-line bg-surface px-5 sm:px-12">
                <ErrorNotice intent="mutation"
                  title="保存系统配置失败"
                  error={saveError}
                  variant="mutation"
                  onRetry={doSave}
                />
              </div>
              <div className="flex gap-3 bg-surface px-5 py-3.5 sm:px-12">
                {/* init=向导：上一步 + 下一步 / 保存，逐级推进。 */}
                {step > 0 && (
                  <button
                    className="cursor-pointer rounded-[11px] border border-line bg-surface px-[22px] py-2.5 text-[15px] text-ink-2"
                    onClick={() => setStep(step - 1)}
                  >
                    上一步
                  </button>
                )}
                <span className="flex-1" />
                <button
                  className="shrink-0 cursor-pointer whitespace-nowrap rounded-[11px] border border-ink-1 bg-ink-1 px-[22px] py-2.5 text-[15px] font-[550] text-surface disabled:opacity-45"
                  onClick={onNext}
                  disabled={nextDisabled || saving}
                >
                  {saving
                    ? copy.savingLabel
                    : step === steps.length - 1
                      ? copy.saveLabel
                      : '下一步'}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
      <DirectoryPicker
        open={directoryPickerTarget !== null}
        initialPath={
          directoryPickerTarget === 'log'
            ? draft.app_log_dir
            : splitLines(draft.algorithm_directories).at(-1)
        }
        onClose={() => setDirectoryPickerTarget(null)}
        onSelect={(path) => {
          if (directoryPickerTarget === 'log') {
            set({ app_log_dir: path })
            return
          }
          const directories = appendUniqueStrings(
            splitLines(draft.algorithm_directories),
            [path],
          )
          set({ algorithm_directories: directories.join('\n') })
        }}
      />
      <ConfirmModal spec={confirm} onClose={() => setConfirm(null)} />
      <Toast />
    </div>
  )
}
