/**
 * 定时任务 → crontab 编译（从原型移植为纯函数）。
 *
 * 第一性原理：执行时刻 = 账户节奏 ∩ 渠道市场钟（左闭右开可报单窗）。
 * 每段开盘先打一枪，再按频率走；收盘点不能下单则钳到最后一分钟。
 * 前台只给频率/补发，时刻从市场钟推出来再编成 crontab（北京时间）。
 * 多条规则用 ``|`` 拼接（后端 `parse_cron_expr` 支持）。
 */

export type ScheduleKind = 'continuous' | 'cn_stock' | 'cn_futures'

export interface NightSchedule {
  label: string
  range_label: string
  close: string[]
  m15: string[]
  m60: string[]
}

/** 自定义节奏可选频率。 */
export type CustomFreq = 'm15' | 'm60' | 'm120' | 'm240' | 'd1'

/** 自定义节奏锚点（日线/4 小时线取开盘或临收）。 */
export type Anchor = 'open' | 'close'

export const SCHEDULE_NAME: Record<ScheduleKind, string> = {
  continuous: '全天候',
  cn_stock: 'A股',
  cn_futures: '期货',
}

/** 各调度类型默认预设 id。 */
export const DEFAULT_PRESET: Record<ScheduleKind, string> = {
  continuous: 'd1',
  cn_futures: 'close',
  cn_stock: 'open',
}

/** 期货快捷节奏默认并入夜盘；A 股 / 连续交易没有夜盘开关。 */
export function defaultNightOn(market: ScheduleKind): boolean {
  return market === 'cn_futures'
}

/** 渠道市场钟（左闭右开）。空数组 = 连续交易。 */
export interface SessionWindow {
  start: string
  end: string
}

const CLOCK_WINDOWS: Record<ScheduleKind, SessionWindow[]> = {
  continuous: [],
  cn_stock: [
    { start: '09:30', end: '11:30' },
    { start: '13:00', end: '15:00' },
  ],
  cn_futures: [
    { start: '09:00', end: '11:30' },
    { start: '13:00', end: '15:15' },
    { start: '21:00', end: '02:30' },
  ],
}

const SESSION_OPEN: Record<Exclude<ScheduleKind, 'continuous'>, string> = {
  cn_stock: '09:30',
  cn_futures: '09:00',
}
const SESSION_CLOSE: Record<Exclude<ScheduleKind, 'continuous'>, string> = {
  cn_stock: '14:50',
  cn_futures: '14:55',
}

function parseHm(value: string): number {
  const [hour, minute] = value.split(':').map(Number)
  return hour * 60 + minute
}

function fromMinutes(total: number): string {
  const wrapped = ((total % (24 * 60)) + 24 * 60) % (24 * 60)
  return `${String(Math.floor(wrapped / 60)).padStart(2, '0')}:${String(wrapped % 60).padStart(2, '0')}`
}

function isOvernight(window: SessionWindow): boolean {
  return parseHm(window.start) >= parseHm(window.end)
}

function spanMinutes(window: SessionWindow): number {
  const start = parseHm(window.start)
  const end = parseHm(window.end)
  return start < end ? end - start : 24 * 60 - start + end
}

function windowContains(window: SessionWindow, label: string): boolean {
  const clock = parseHm(label)
  const start = parseHm(window.start)
  const end = parseHm(window.end)
  if (start < end) return start <= clock && clock < end
  return clock >= start || clock < end
}

function hhmmInWindows(label: string, windows: SessionWindow[]): boolean {
  return windows.length === 0 || windows.some((window) => windowContains(window, label))
}

function barWindows(kind: ScheduleKind, windows: SessionWindow[]): SessionWindow[] {
  if (kind !== 'cn_futures') return windows
  return windows.map((window) => (window.start === '13:00' && window.end === '15:15' ? { start: window.start, end: '15:00' } : window))
}

function rhythmTimes(windows: SessionWindow[], freq: number, clock: SessionWindow[] = windows): string[] {
  const seen: string[] = []
  const known = new Set<string>()
  for (const window of windows) {
    const begin = parseHm(window.start)
    const span = spanMinutes(window)
    const bases: number[] = []
    let step = 0
    while (step < span) {
      bases.push(step)
      step += freq
    }
    if (step === span) {
      const boundary = fromMinutes(begin + span)
      bases.push(hhmmInWindows(boundary, clock) ? span : span - 1)
    }
    for (const base of bases) {
      const label = fromMinutes(begin + base)
      if (!hhmmInWindows(label, clock) || known.has(label)) continue
      known.add(label)
      seen.push(label)
    }
  }
  return seen
}

function applyOffsets(times: string[], windows: SessionWindow[], offs: number[]): string[] {
  const seen: string[] = []
  const known = new Set<string>()
  for (const time of times) {
    for (const offset of offs) {
      const [hour, minute] = time.split(':').map(Number)
      const label = fromMinutes(hour * 60 + minute + offset)
      if ((offset !== 0 && !hhmmInWindows(label, windows)) || known.has(label)) continue
      known.add(label)
      seen.push(label)
    }
  }
  return seen
}

function sessionTimes(kind: Exclude<ScheduleKind, 'continuous'>, freq: 15 | 60 | 120): string[] {
  const clock = CLOCK_WINDOWS[kind]
  return rhythmTimes(
    barWindows(kind, clock).filter((window) => !isOvernight(window)),
    freq,
    clock,
  )
}

/** 补发偏移集合：0（到点）+ N 次每隔 M 分。 */
function supOffsets(supN: number, supM: number): number[] {
  return [0, ...Array.from({ length: supN }, (_, k) => (k + 1) * supM)]
}

/** 连续交易：分钟基点 + 偏移 → 同小时内分钟列表。 */
function continuousMinutes(base: number[], offs: number[]): string {
  return [...new Set(base.flatMap((b) => offs.map((o) => (b + o) % 60)))].sort((a, b) => a - b).join(',')
}

/** 一组 HH:MM × 偏移集 → 按分钟分组的 cron 列表。 */
function timesToCron(times: string[], dow: string, offs: number[], windows: SessionWindow[] = []): string[] {
  const points = windows.length ? applyOffsets(times, windows, offs) : times.flatMap((time) => {
    const [hour, minute] = time.split(':').map(Number)
    return offs.map((offset) => fromMinutes(hour * 60 + minute + offset))
  })
  const byMin: Record<number, number[]> = {}
  for (const point of points) {
    const [hour, minute] = point.split(':').map(Number)
    ;(byMin[minute] = byMin[minute] || []).push(hour)
  }
  return Object.keys(byMin)
    .map(Number)
    .sort((a, b) => a - b)
    .map((minute) => `${minute} ${[...new Set(byMin[minute])].sort((a, b) => a - b).join(',')} * * ${dow}`)
}

export interface Preset {
  id: string
  label: string
  sub?: string
  build: (offs: number[]) => string[]
}

/** 市场感知的快捷预设。 */
export const PRESETS: Record<ScheduleKind, Preset[]> = {
  continuous: [
    { id: 'm15', label: '每 15 分钟', sub: '每 15 分', build: (o) => [`${continuousMinutes([0, 15, 30, 45], o)} * * * *`] },
    { id: 'h1', label: '每小时', sub: ':00 起', build: (o) => [`${continuousMinutes([0], o)} * * * *`] },
    { id: 'h4', label: '每 4 小时', sub: '*/4 时', build: (o) => [`${continuousMinutes([0], o)} */4 * * *`] },
    { id: 'd1', label: '每天', sub: '08:00', build: (o) => [`${continuousMinutes([0], o)} 8 * * *`] },
  ],
  cn_stock: [
    { id: 'open', label: '每交易日 · 开盘', sub: '09:30', build: (o) => timesToCron([SESSION_OPEN.cn_stock], '*', o, CLOCK_WINDOWS.cn_stock) },
    { id: 'close', label: '每交易日 · 临收', sub: '14:50', build: (o) => timesToCron([SESSION_CLOSE.cn_stock], '*', o, CLOCK_WINDOWS.cn_stock) },
    { id: 'm15', label: '盘中每 15 分', build: (o) => timesToCron(sessionTimes('cn_stock', 15), '*', o, CLOCK_WINDOWS.cn_stock) },
    { id: 'm60', label: '盘中每 60 分', build: (o) => timesToCron(sessionTimes('cn_stock', 60), '*', o, CLOCK_WINDOWS.cn_stock) },
  ],
  cn_futures: [
    { id: 'close', label: '日盘收盘', sub: '14:55', build: (o) => timesToCron([SESSION_CLOSE.cn_futures], '*', o, CLOCK_WINDOWS.cn_futures) },
    { id: 'm15', label: '日盘每 15 分', build: (o) => timesToCron(sessionTimes('cn_futures', 15), '*', o, CLOCK_WINDOWS.cn_futures) },
    { id: 'm60', label: '日盘每 60 分', build: (o) => timesToCron(sessionTimes('cn_futures', 60), '*', o, CLOCK_WINDOWS.cn_futures) },
  ],
}

/** 由所选预设 + 补发参数编译出 cron 列表。 */
export function buildCronList(
  market: ScheduleKind,
  presetIds: string[],
  supN: number,
  supM: number,
  night?: NightSchedule | null,
  nightOn = false,
): string[] {
  const offs = supOffsets(supN, supM)
  let out: string[] = []
  for (const id of presetIds) {
    const p = PRESETS[market].find((x) => x.id === id)
    if (p) out = out.concat(p.build(offs))
    if (nightOn && night && (id === 'close' || id === 'm15' || id === 'm60')) {
      out = out.concat(timesToCron(night[id], '*', offs, CLOCK_WINDOWS.cn_futures))
    }
  }
  return [...new Set(out)]
}

/* ------------------ 高级：结构化时间规则 ------------------ */

/** 定时页 tab。 */
export type TimerTab = 'quick' | 'advanced' | 'custom'

/**
 * 高级编辑器中的一条时间规则。
 *
 * ``draft: true`` 表示空槽（未填完），编译时跳过；日频须有合法 ``time``。
 */
export interface ScheduleRule {
  id: string
  freq: CustomFreq
  /** HH:MM；日频必填，周期类可忽略。 */
  time: string
  /** 周几掩码：0=日 … 6=六；空数组 = 调度默认（连续交易每天 / 时段市场工作日）。 */
  days: number[]
  /** 时段市场日频/4h 锚点（有合法 time 时优先用 time）。 */
  anchor: Anchor
  /** 空槽：未完成，不参与编译。 */
  draft: boolean
}

const FREQ_LABEL: Record<CustomFreq, string> = {
  m15: '每 15 分钟',
  m60: '每小时',
  m120: '每 2 小时',
  m240: '每 4 小时',
  d1: '每天',
}

/** 生成规则 id。 */
export function newRuleId(): string {
  return `r_${Math.random().toString(36).slice(2, 10)}`
}

/** 校验 HH:MM。 */
export function isValidTime(time: string): boolean {
  const m = /^(\d{1,2}):(\d{2})$/.exec(time.trim())
  if (!m) return false
  const h = Number(m[1])
  const min = Number(m[2])
  return h >= 0 && h <= 23 && min >= 0 && min <= 59
}

/** 规则是否可编译（非空槽且参数齐全）。 */
export function isRuleComplete(rule: ScheduleRule): boolean {
  if (rule.draft) return false
  if (rule.freq === 'd1') return isValidTime(rule.time)
  return true
}

/** UI 周几（0=日…6=六）→ APScheduler cron 第 5 段（0=一…6=日）。 */
export function daysToCronField(days: number[]): string {
  if (!days.length) return '*'
  const sorted = [...new Set(days)].sort((a, b) => a - b)
  if (sorted.length === 7) return '*'
  return sorted.map((day) => (day === 0 ? 6 : day - 1)).sort((a, b) => a - b).join(',')
}

/** 市场默认一条完整规则（进高级 / 重置用）。 */
export function defaultScheduleRule(market: ScheduleKind): ScheduleRule {
  if (market === 'continuous') {
    return { id: newRuleId(), freq: 'd1', time: '08:00', days: [], anchor: 'open', draft: false }
  }
  if (market === 'cn_stock') {
    return { id: newRuleId(), freq: 'd1', time: '09:30', days: [], anchor: 'open', draft: false }
  }
  return { id: newRuleId(), freq: 'd1', time: SESSION_CLOSE.cn_futures, days: [], anchor: 'close', draft: false }
}

/** 由快捷预设 id 展开为一条高级规则。 */
export function ruleFromPreset(market: ScheduleKind, presetId: string): ScheduleRule {
  const base = defaultScheduleRule(market)
  if (market === 'continuous') {
    if (presetId === 'm15') return { ...base, id: newRuleId(), freq: 'm15', time: '00:00' }
    if (presetId === 'h1') return { ...base, id: newRuleId(), freq: 'm60', time: '00:00' }
    if (presetId === 'h4') return { ...base, id: newRuleId(), freq: 'm240', time: '00:00' }
    return { ...base, id: newRuleId(), freq: 'd1', time: '08:00' }
  }
  if (market === 'cn_stock') {
    if (presetId === 'm15') return { ...base, id: newRuleId(), freq: 'm15', time: '00:00' }
    if (presetId === 'm60') return { ...base, id: newRuleId(), freq: 'm60', time: '00:00' }
    if (presetId === 'close') return { ...base, id: newRuleId(), freq: 'd1', time: '14:50', anchor: 'close' }
    return { ...base, id: newRuleId(), freq: 'd1', time: '09:30', anchor: 'open' }
  }
  if (presetId === 'm15') return { ...base, id: newRuleId(), freq: 'm15', time: '00:00' }
  if (presetId === 'm60') return { ...base, id: newRuleId(), freq: 'm60', time: '00:00' }
  return { ...base, id: newRuleId(), freq: 'd1', time: SESSION_CLOSE.cn_futures, anchor: 'close' }
}

/**
 * 基于已有规则生成空槽（进组合时的第 2 项 / 「再加一条」）。
 * 继承重复维度，日频时刻留空待填。
 */
export function makeEmptySlot(from: ScheduleRule): ScheduleRule {
  return {
    id: newRuleId(),
    freq: 'd1',
    time: '',
    days: [...from.days],
    anchor: from.anchor,
    draft: true,
  }
}

/** 规则人话短标签（右栏列表用）。 */
export function describeRule(rule: ScheduleRule): string {
  if (rule.draft && !isValidTime(rule.time)) return '未完成'
  const dayBit = rule.days.length && rule.days.length < 7
    ? `周${rule.days
        .slice()
        .sort((a, b) => a - b)
        .map((d) => '日一二三四五六'[d])
        .join('')}`
    : ''
  if (rule.freq === 'd1') {
    const t = isValidTime(rule.time) ? rule.time : '—:—'
    return dayBit ? `${dayBit} ${t}` : `每天 ${t}`
  }
  const core = FREQ_LABEL[rule.freq]
  return dayBit ? `${core} · ${dayBit}` : core
}

/**
 * 将一条结构化规则编译为 crontab。
 *
 * 日频用 ``time``；周期类按市场 bar 表或连续交易整点表达式；``days`` 写入周字段。
 */
export function compileScheduleRule(
  market: ScheduleKind,
  rule: ScheduleRule,
  supN: number,
  supM: number,
): string[] {
  if (!isRuleComplete(rule)) return []
  const offs = supOffsets(supN, supM)
  const dow = daysToCronField(rule.days)

  if (market === 'continuous') {
    if (rule.freq === 'd1') {
      return timesToCron([rule.time.trim()], dow, offs)
    }
    const base: Record<Exclude<CustomFreq, 'd1'>, number[]> = {
      m15: [0, 15, 30, 45],
      m60: [0],
      m120: [0],
      m240: [0],
    }
    const hr: Record<Exclude<CustomFreq, 'd1'>, string> = {
      m15: '*',
      m60: '*',
      m120: '*/2',
      m240: '*/4',
    }
    const f = rule.freq as Exclude<CustomFreq, 'd1'>
    return [`${continuousMinutes(base[f], offs)} ${hr[f]} * * ${dow}`]
  }

  const clock = CLOCK_WINDOWS[market]
  const kind = market as Exclude<ScheduleKind, 'continuous'>
  if (rule.freq === 'd1' || rule.freq === 'm240') {
    if (rule.freq === 'd1' && isValidTime(rule.time)) {
      return timesToCron([rule.time.trim()], dow, offs, clock)
    }
    const t = rule.anchor === 'close' ? SESSION_CLOSE[kind] : SESSION_OPEN[kind]
    return timesToCron([t], dow, offs, clock)
  }
  const freq = rule.freq === 'm15' ? 15 : rule.freq === 'm120' ? 120 : 60
  return timesToCron(sessionTimes(kind, freq), dow, offs, clock)
}

/** 编译高级规则列表（跳过空槽）。 */
export function compileScheduleRules(
  market: ScheduleKind,
  rules: ScheduleRule[],
  supN: number,
  supM: number,
): string[] {
  return rules.flatMap((r) => compileScheduleRule(market, r, supN, supM))
}

/**
 * 定时「意图」：快捷预设 / 高级规则列表 / 裸 cron / 补发。

 * Notes
 * -----
 * 向导跨步骤保存此意图以便回显与再编辑；``cron_expr`` 由 :func:`resolveCronList` 现算。
 * 当前 tab 是唯一生效来源：自定义表达式 / 高级规则 / 快捷预设。
 */
export interface TimerIntent {
  presetIds: string[]
  nightOn?: boolean
  supN: number
  supM: number
  rawCron: string
  /** 当前生效的定时模式：快捷 | 高级 | 自定义。 */
  timerTab: TimerTab
  /** 高级：时间规则列表；长度 ≥2 时 UI 显示右栏组合列表。 */
  scheduleRules?: ScheduleRule[]
  /** 高级右栏当前选中的规则 id。 */
  selectedRuleId?: string
}

/**
 * 按当前 tab 收敛出 cron 列表。

 * Parameters
 * ----------
 * market : ScheduleKind
 *     目标市场。
 * t : TimerIntent
 *     定时意图。
 *
 * Returns
 * -------
 * string[]
 *     crontab 规则列表；均为空时返回空数组。
 */
export function resolveCronList(market: ScheduleKind, t: TimerIntent, night?: NightSchedule | null): string[] {
  const raw = t.rawCron.trim()
  if (t.timerTab === 'custom') {
    return raw
      .split('|')
      .map((s) => s.trim())
      .filter(Boolean)
  }
  if (t.timerTab === 'advanced' && t.scheduleRules?.length) {
    return compileScheduleRules(market, t.scheduleRules, t.supN, t.supM)
  }
  return buildCronList(market, t.presetIds, t.supN, t.supM, night, Boolean(t.nightOn))
}

/**
 * 校验裸 crontab：每条规则须为 5 段（分 时 日 月 周）。

 * Parameters
 * ----------
 * raw : str
 *     裸 crontab 文本，多条用 ``|`` 分隔。

 * Returns
 * -------
 * str | None
 *     不合法时返回错误提示；合法或为空时返回 ``None``。
 */
export function cronError(raw: string): string | null {
  const rules = raw
    .split('|')
    .map((s) => s.trim())
    .filter(Boolean)
  const bad = rules.some((s) => s.split(/\s+/).length !== 5)
  return bad ? '自定义节奏格式有误：每条规则须包含 5 项（分、时、日、月、星期）。' : null
}

/** 拼成后端要的单字符串（`|` 分隔）。 */
export function cronToExpr(list: string[]): string {
  return list.join(' | ')
}

/* ------------------ 下次触发预览 ------------------ */

function matchField(expr: string, val: number): boolean {
  if (expr === '*') return true
  return expr.split(',').some((p) => {
    if (p.includes('/')) {
      const [rng, stp] = p.split('/')
      const n = parseInt(stp, 10)
      let lo = 0
      let hi = 59
      if (rng !== '*') {
        if (rng.includes('-')) {
          const a = rng.split('-')
          lo = +a[0]
          hi = +a[1]
        } else lo = hi = +rng
      }
      return val >= lo && val <= hi && (val - lo) % n === 0
    }
    if (p.includes('-')) {
      const a = p.split('-')
      return val >= +a[0] && val <= +a[1]
    }
    return +p === val
  })
}

function cronMatch(f: string[], d: Date): boolean {
  const jsDay = d.getDay()
  const apschedulerDay = jsDay === 0 ? 6 : jsDay - 1
  return (
    matchField(f[0], d.getMinutes()) &&
    matchField(f[1], d.getHours()) &&
    matchField(f[2], d.getDate()) &&
    matchField(f[3], d.getMonth() + 1) &&
    matchField(f[4], apschedulerDay)
  )
}

/** 计算未来 count 个触发时刻（本地时钟近似，用于预览）。 */
export function nextFires(list: string[], count: number): Date[] {
  const F = list.map((c) => c.trim().split(/\s+/)).filter((a) => a.length === 5)
  if (!F.length) return []
  const res: Date[] = []
  const d = new Date()
  d.setSeconds(0, 0)
  d.setMinutes(d.getMinutes() + 1)
  for (let i = 0; i < 60 * 24 * 40 && res.length < count; i++) {
    if (F.some((f) => cronMatch(f, d))) res.push(new Date(d))
    d.setMinutes(d.getMinutes() + 1)
  }
  return res
}

/** 格式化触发时刻为「今天/明天 HH:MM」。 */
export function fmtFire(d: Date): string {
  const now = new Date()
  const a = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const b = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const diff = Math.round((b.getTime() - a.getTime()) / 864e5)
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const day =
    diff === 0 ? '今天' : diff === 1 ? '明天' : diff === 2 ? '后天' : `周${'日一二三四五六'[d.getDay()]} ${d.getMonth() + 1}/${d.getDate()}`
  return `${day} ${hm}`
}

/* ------------------ cron → 人话反解 ------------------ */

/** 规范化单条 cron：把逗号列表排序去重，其余段原样，便于与正向编译产物字符串比对。 */
function normCronLine(line: string): string {
  const seg = line.trim().split(/\s+/)
  if (seg.length !== 5) return line.trim()
  return seg
    .map((s) => (s.includes(',') ? [...new Set(s.split(','))].map(Number).sort((a, b) => a - b).join(',') : s))
    .join(' ')
}

/** 规范化整份 cron（多条以 `|` 或换行分隔）为可比较的稳定串。 */
function normCronSet(expr: string): string {
  return [...new Set(expr.split(/[|\n]/).map(normCronLine).filter(Boolean))].sort().join(' | ')
}

/** 补发档位（与设置向导下拉一致），用于穷举反解。 */
const SUP_M = [1, 2, 3, 5]

interface FixedCronTime {
  time: string
  dow: string
}

/** APScheduler 周字段→人话；仅接受 ``*`` 或显式数字列表。 */
function describeDow(dow: string): string | null {
  if (dow === '*') return '每天'
  if (!/^\d(?:,\d)*$/.test(dow)) return null
  const days = [...new Set(dow.split(',').map(Number))].sort((a, b) => a - b)
  if (!days.length || days.some((day) => day < 0 || day > 6)) return null
  if (days.length === 7) return '每天'
  const labels = ['一', '二', '三', '四', '五', '六', '日']
  return `每周${days.map((day) => labels[day]).join('、')}`
}

/** 仅解析无日/月限制的单一固定时刻。 */
function parseFixedCronTime(line: string): FixedCronTime | null {
  const [minute, hour, dom, month, dow, ...rest] = line.trim().split(/\s+/)
  if (rest.length || dom !== '*' || month !== '*' || describeDow(dow) == null) return null
  if (!/^\d{1,2}$/.test(minute) || !/^\d{1,2}$/.test(hour)) return null
  const m = Number(minute)
  const h = Number(hour)
  if (m > 59 || h > 23) return null
  return { time: `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`, dow }
}

/** 常见五段 Cron 的保守型人话描述；无法无损表达时返回 ``null``。 */
function describeCommonCron(expr: string): string | null {
  const lines = expr.split(/[|\n]/).map((line) => line.trim()).filter(Boolean)
  if (!lines.length) return null

  const fixed = lines.map(parseFixedCronTime)
  if (fixed.every((item): item is FixedCronTime => item != null)) {
    const dow = fixed[0]!.dow
    if (!fixed.every((item) => item.dow === dow)) return null
    const prefix = describeDow(dow)
    if (!prefix) return null
    const times = [...new Set(fixed.map((item) => item.time))].sort()
    return `${prefix} ${times.join('、')}`
  }

  if (lines.length !== 1) return null
  const [minute, hour, dom, month, dow, ...rest] = lines[0]!.split(/\s+/)
  if (rest.length || dom !== '*' || month !== '*' || dow !== '*') return null
  const minuteStep = minute.match(/^\*\/(\d{1,2})$/)
  if (minuteStep && hour === '*') {
    const step = Number(minuteStep[1])
    return step >= 1 && step <= 59 ? `每 ${step} 分钟` : null
  }
  if (minute === '0' && hour === '*') return '每小时'
  const hourStep = hour.match(/^\*\/(\d{1,2})$/)
  if (minute === '0' && hourStep) {
    const step = Number(hourStep[1])
    return step >= 1 && step <= 23 ? `每 ${step} 小时` : null
  }
  return null
}

/**
 * 反解基础 cron 为设置向导同款人话。
 *
 * 仅匹配基础预设，其余尝试通用五段描述，不从偏移推断补发。
 *
 * @param market 目标市场，决定候选预设集。
 * @param cronExpr 存储的 crontab（多条以 `|` 或换行分隔）。
 * @returns 人话短语；无法归类时为 ``null``。
 */
export function describeCron(
  market: ScheduleKind,
  cronExpr: string,
  night?: NightSchedule | null,
): string | null {
  const target = normCronSet(cronExpr)
  if (!target) return null
  for (const p of PRESETS[market]) {
    for (const n of [0]) {
      for (const m of SUP_M) {
        for (const nightOn of night ? [false, true] : [false]) {
          if (normCronSet(buildCronList(market, [p.id], n, m, night, nightOn).join(' | ')) !== target) continue
          const time = p.sub && /^\d{1,2}:\d{2}$/.test(p.sub) ? ` ${p.sub}` : ''
          const nightText = nightOn ? ` · ${night?.label ?? '夜盘'}` : ''
          return n > 0 ? `${p.label}${time}${nightText} · 补发 ${n} 次` : `${p.label}${time}${nightText}`
        }
      }
    }
  }
  return describeCommonCron(target)
}

/* ------------------ cron → 编辑器意图反解（账户编辑页） ------------------ */

/**
 * 定时编辑器完整状态：在 :interface:`TimerIntent` 上增加「自动调仓」总开关。
 *
 * 空 ``cron_expr`` 对应 ``autoOn: false``（仅手动）；有表达式则 ``autoOn: true`` 并尽量回填
 * 快捷预设 / 高级规则 / 自定义模式。
 */
export interface TimerEditorState extends TimerIntent {
  autoOn: boolean
  nightOn: boolean
  timerTab: TimerTab
  scheduleRules: ScheduleRule[]
  selectedRuleId: string
}

/** 构造一份「关自动 + 市场默认节奏」草稿（打开开关后可立刻用）。 */
export function defaultTimerEditorState(market: ScheduleKind): TimerEditorState {
  const rule = defaultScheduleRule(market)
  return {
    autoOn: false,
    presetIds: [DEFAULT_PRESET[market]],
    nightOn: defaultNightOn(market),
    supN: 0,
    supM: 1,
    rawCron: '',
    timerTab: 'quick',
    scheduleRules: [rule],
    selectedRuleId: rule.id,
  }
}

/** 解析 cron 第 5 段（周）为 days 掩码；无法识别返回 null。 */
function parseDowField(dow: string): number[] | null {
  if (dow === '*') return []
  if (!/^\d+(,\d+)*$/.test(dow)) return null
  const cronDays = [...new Set(dow.split(',').map(Number))].filter((d) => d >= 0 && d <= 6)
  const days = cronDays.map((day) => (day === 6 ? 0 : day + 1)).sort((a, b) => a - b)
  return days.length ? days : null
}

/**
 * 尝试把「每条均为单一时刻」的 cron 反解成高级日频规则列表。
 *
 * 仅支持 ``M H * * DOW``（分/时为单个数字）；列表分钟、步长、DOM/MON 非 ``*`` 时放弃。
 */
function tryParseDailyRules(cronExpr: string): ScheduleRule[] | null {
  const lines = cronExpr
    .split(/[|\n]/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (!lines.length) return null
  const rules: ScheduleRule[] = []
  for (const line of lines) {
    const parts = line.split(/\s+/)
    if (parts.length !== 5) return null
    const [min, hour, dom, mon, dow] = parts
    if (dom !== '*' || mon !== '*') return null
    if (!/^\d{1,2}$/.test(min) || !/^\d{1,2}$/.test(hour)) return null
    const days = parseDowField(dow)
    if (days == null) return null
    const h = Number(hour)
    const m = Number(min)
    if (h > 23 || m > 59) return null
    const time = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
    rules.push({
      id: newRuleId(),
      freq: 'd1',
      time,
      days,
      anchor: 'open',
      draft: false,
    })
  }
  return rules
}

/**
 * 仅匹配基础预设，不从旧 cron 推断补发。
 */
function tryMatchPreset(
  market: ScheduleKind,
  target: string,
  night?: NightSchedule | null,
): Pick<TimerEditorState, 'presetIds' | 'nightOn' | 'supN' | 'supM'> | null {
  for (const p of PRESETS[market]) {
    for (const n of [0]) {
      for (const m of SUP_M) {
        for (const nightOn of night ? [false, true] : [false]) {
          if (normCronSet(buildCronList(market, [p.id], n, m, night, nightOn).join(' | ')) === target) {
            return { presetIds: [p.id], nightOn, supN: n, supM: m }
          }
        }
      }
    }
  }
  return null
}

/**
 * 从存储的 ``cron_expr`` 反解为定时编辑器状态（向导 / 账户编辑共用）。
 *
 * 优先级：空 → 关自动；单基础预设 → 快捷；纯日频多时刻 → 高级规则；其余 → 高级+自定义表达式。
 *
 * Parameters
 * ----------
 * market : ScheduleKind
 *     账户市场，决定预设集与默认规则。
 * cronExpr : str
 *     后端存的 crontab（``|`` 或换行分隔）。
 *
 * Returns
 * -------
 * TimerEditorState
 *     可直接喂给 :component:`TimerEditor` 的完整状态。
 */
export function parseTimerIntent(
  market: ScheduleKind,
  cronExpr: string,
  night?: NightSchedule | null,
): TimerEditorState {
  const trimmed = (cronExpr ?? '').trim()
  if (!trimmed) return defaultTimerEditorState(market)

  const target = normCronSet(trimmed)
  const hit = tryMatchPreset(market, target, night)
  if (hit) {
    const rule = ruleFromPreset(market, hit.presetIds[0]!)
    return {
      autoOn: true,
      ...hit,
      rawCron: '',
      timerTab: 'quick',
      scheduleRules: [rule],
      selectedRuleId: rule.id,
    }
  }

  const daily = tryParseDailyRules(trimmed)
  if (daily?.length) {
    return {
      autoOn: true,
      presetIds: [DEFAULT_PRESET[market]],
      nightOn: false,
      supN: 0,
      supM: 1,
      rawCron: '',
      timerTab: 'advanced',
      scheduleRules: daily,
      selectedRuleId: daily[0]!.id,
    }
  }

  // 无法结构化：进高级自定义模式，保留原表达式（| 归一）。
  const rule = defaultScheduleRule(market)
  const raw = trimmed
    .split(/[|\n]/)
    .map((s) => s.trim())
    .filter(Boolean)
    .join(' | ')
  return {
    autoOn: true,
    presetIds: [DEFAULT_PRESET[market]],
    nightOn: false,
    supN: 0,
    supM: 1,
    rawCron: raw,
    timerTab: 'custom',
    scheduleRules: [rule],
    selectedRuleId: rule.id,
  }
}

/**
 * 将编辑器状态编译为后端 ``cron_expr``；关自动返回空串。
 */
export function timerStateToCronExpr(
  market: ScheduleKind,
  state: TimerEditorState,
  night?: NightSchedule | null,
): string {
  if (!state.autoOn) return ''
  return cronToExpr(resolveCronList(market, { ...state, supN: 0 }, night))
}

/**
 * 比较两份 cron 表达式是否语义相同（忽略分隔符、空白、条目顺序与逗号段内排序）。
 */
export function cronExprEqual(a: string, b: string): boolean {
  return normCronSet(a) === normCronSet(b)
}

/** 自定义模式的内容错误；无则 null。 */
export function timerEditorError(state: TimerEditorState): string | null {
  if (!state.autoOn || state.timerTab !== 'custom') return null
  return state.rawCron.trim() ? cronError(state.rawCron) : '自定义节奏不能为空。'
}


export type Supplement = { count: number; interval_minutes: number }

/** 读取独立配置；旧表达式绝不反推补发关系。 */
export function parseSavedTimer(market: ScheduleKind, cronExpr: string, night?: NightSchedule | null, supplement?: Supplement | null): TimerEditorState {
  const parsed = parseTimerIntent(market, cronExpr, night)
  return { ...parsed, supN: supplement?.count ?? 0, supM: supplement?.interval_minutes ?? 1 }
}

export function timerSupplement(state: Pick<TimerEditorState, 'autoOn' | 'supN' | 'supM'>): Supplement | null {
  return state.autoOn && state.supN > 0 ? { count: state.supN, interval_minutes: state.supM } : null
}
