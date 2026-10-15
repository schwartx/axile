import { describe, it, expect, setSystemTime } from 'bun:test'

import {
  buildCronList,
  compileScheduleRule,
  defaultNightOn,
  defaultTimerEditorState,
  describeCron,
  describeRule,
  makeEmptySlot,
  nextFires,
  parseTimerIntent,
  parseSavedTimer,
  timerSupplement,
  resolveCronList,
  timerStateToCronExpr,
  type ScheduleRule,
  type TimerIntent,
} from './cron'
import { timerEditorError } from './cron'

/** 构造一个定时意图，默认无补发（supN=0）便于断言精确值。 */
function intent(patch: Partial<TimerIntent> = {}): TimerIntent {
  return {
    presetIds: [],
    supN: 0,
    supM: 0,
    rawCron: '',
    timerTab: 'quick',
    scheduleRules: [],
    selectedRuleId: '',
    ...patch,
  }
}

function rule(patch: Partial<ScheduleRule> = {}): ScheduleRule {
  return {
    id: 'r1',
    freq: 'd1',
    time: '08:00',
    days: [],
    anchor: 'open',
    draft: false,
    ...patch,
  }
}

describe('compileScheduleRule · continuous', () => {
  it('日线锚定 08:00（无补发）', () => {
    expect(compileScheduleRule('continuous', rule({ freq: 'd1', time: '08:00' }), 0, 0)).toEqual(['0 8 * * *'])
  })

  it('m60 走每小时、m120 走 */2、m240 走 */4', () => {
    expect(compileScheduleRule('continuous', rule({ freq: 'm60' }), 0, 0)).toEqual(['0 * * * *'])
    expect(compileScheduleRule('continuous', rule({ freq: 'm120' }), 0, 0)).toEqual(['0 */2 * * *'])
    expect(compileScheduleRule('continuous', rule({ freq: 'm240' }), 0, 0)).toEqual(['0 */4 * * *'])
  })

  it('m15 叠加补发（补 2 次每隔 1 分）铺满每 15 分的 0/1/2 偏移', () => {
    expect(compileScheduleRule('continuous', rule({ freq: 'm15' }), 2, 1)).toEqual([
      '0,1,2,15,16,17,30,31,32,45,46,47 * * * *',
    ])
  })
})

describe('compileScheduleRule · 时段市场', () => {
  it('A股日线 open=09:30 / close=14:50', () => {
    expect(compileScheduleRule('cn_stock', rule({ freq: 'd1', time: '09:30' }), 0, 0)).toEqual(['30 9 * * *'])
    expect(compileScheduleRule('cn_stock', rule({ freq: 'd1', time: '14:50', anchor: 'close' }), 0, 0)).toEqual(['50 14 * * *'])
  })

  it('CTP 日线 close=14:55', () => {
    expect(compileScheduleRule('cn_futures', rule({ freq: 'd1', time: '14:55', anchor: 'close' }), 0, 0)).toEqual(['55 14 * * *'])
  })

  it('m240 与日线同锚点', () => {
    expect(compileScheduleRule('cn_stock', rule({ freq: 'm240', anchor: 'close' }), 0, 0)).toEqual(['50 14 * * *'])
  })

  it('A股 m120 开盘打一枪，收盘钳到可报单最后一分钟', () => {
    expect(compileScheduleRule('cn_stock', rule({ freq: 'm120' }), 0, 0)).toEqual([
      '0 13 * * *',
      '29 11 * * *',
      '30 9 * * *',
      '59 14 * * *',
    ])
  })
})

describe('期货渠道夜盘快捷节奏', () => {
  const night = {
    label: '夜盘',
    range_label: '21:00–次日 02:30',
    close: ['02:30'],
    m15: ['21:15', '21:30', '21:45', '22:00', '02:30'],
    m60: ['22:00', '23:00', '00:00', '01:00', '02:00', '02:30'],
  }

  it('关闭时保持既有日盘表达式，开启时合并渠道夜盘时点', () => {
    expect(buildCronList('cn_futures', ['close'], 0, 1, night, false)).toEqual(['55 14 * * *'])
    expect(buildCronList('cn_futures', ['close'], 0, 1, night, true)).toEqual([
      '55 14 * * *',
      '30 2 * * *',
    ])
  })

  it('夜盘快捷表达式可无损反解开关状态', () => {
    const expr = buildCronList('cn_futures', ['m60'], 0, 1, night, true).join(' | ')
    const state = parseTimerIntent('cn_futures', expr, night)

    expect(state.timerTab).toBe('quick')
    expect(state.presetIds).toEqual(['m60'])
    expect(state.nightOn).toBe(true)
    expect(timerStateToCronExpr('cn_futures', state, night)).toBe(expr)
  })

  it('期货草稿默认开夜盘，A 股 / 连续交易默认关', () => {
    expect(defaultNightOn('cn_futures')).toBe(true)
    expect(defaultNightOn('cn_stock')).toBe(false)
    expect(defaultNightOn('continuous')).toBe(false)
    expect(defaultTimerEditorState('cn_futures').nightOn).toBe(true)
    expect(defaultTimerEditorState('cn_stock').nightOn).toBe(false)
  })

  it('空 cron 期货草稿带夜盘，已有日盘-only 表达式仍关', () => {
    expect(parseTimerIntent('cn_futures', '', night).nightOn).toBe(true)
    expect(parseTimerIntent('cn_futures', '55 14 * * *', night).nightOn).toBe(false)
  })
})

describe('compileScheduleRule · 可调时刻与周几', () => {
  it('连续交易每天 09:30', () => {
    expect(compileScheduleRule('continuous', rule({ time: '09:30' }), 0, 0)).toEqual(['30 9 * * *'])
  })

  it('显式选择周一至周五时转换为 APScheduler 编号', () => {
    expect(compileScheduleRule('continuous', rule({ time: '08:00', days: [1, 2, 3, 4, 5] }), 0, 0)).toEqual([
      '0 8 * * 0,1,2,3,4',
    ])
  })

  it('显式选择周日时转换为 APScheduler 的 6', () => {
    expect(compileScheduleRule('continuous', rule({ days: [0] }), 0, 0)).toEqual(['0 8 * * 6'])
  })

  it('空槽不编译', () => {
    expect(compileScheduleRule('continuous', rule({ draft: true, time: '' }), 0, 0)).toEqual([])
  })

  it('多条规则拼接（忽略空槽）', () => {
    const t = intent({
      timerTab: 'advanced',
      scheduleRules: [rule({ time: '08:00' }), rule({ id: 'r2', time: '20:00' }), makeEmptySlot(rule())],
    })
    expect(resolveCronList('continuous', t)).toEqual(['0 8 * * *', '0 20 * * *'])
  })
})

describe('resolveCronList · 优先级', () => {
  it('自定义 tab 只使用 rawCron', () => {
    const t = intent({
      rawCron: '5 9 * * 1-5 | 50 14 * * 1-5',
      timerTab: 'custom',
      scheduleRules: [rule()],
      presetIds: ['d1'],
    })
    expect(resolveCronList('continuous', t)).toEqual(['5 9 * * 1-5', '50 14 * * 1-5'])
  })

  it('高级 tab 使用单条结构化规则', () => {
    const t = intent({ timerTab: 'advanced', scheduleRules: [rule()] })
    expect(resolveCronList('continuous', t)).toEqual(['0 8 * * *'])
  })

  it('快捷 tab → 预设编译', () => {
    const t = intent({ presetIds: ['d1'], timerTab: 'quick' })
    expect(resolveCronList('continuous', t)).toEqual(['0 8 * * *'])
  })

  it('空意图返回空数组', () => {
    expect(resolveCronList('continuous', intent())).toEqual([])
  })

  it('自定义 tab 内容为空时不回退到高级规则', () => {
    const t = intent({ timerTab: 'custom', scheduleRules: [rule()] })
    expect(resolveCronList('continuous', t)).toEqual([])
  })
})

describe('buildCronList · 补发放大偏移', () => {
  it('连续交易每 15 分 + 补 2 次每隔 1 分', () => {
    expect(buildCronList('continuous', ['m15'], 2, 1)).toEqual([
      '0,1,2,15,16,17,30,31,32,45,46,47 * * * *',
    ])
  })

  it('A股多选预设拼接多条规则（开盘+临收）', () => {
    expect(buildCronList('cn_stock', ['open', 'close'], 0, 0)).toEqual(['30 9 * * *', '50 14 * * *'])
  })

  it('A股 15 分补发不走进午休', () => {
    const expr = buildCronList('cn_stock', ['m15'], 4, 1).join(' | ')
    expect(expr).toContain('29 11')
    expect(expr).not.toContain('30 11')
    expect(expr).not.toContain('31 11')
    expect(expr).not.toContain('0 15')
  })

  it('期货 15 分下午从 13:00 开盘打一枪', () => {
    const lines = buildCronList('cn_futures', ['m15'], 0, 0)
    expect(lines.some((line) => line.startsWith('0 ') && /\b13\b/.test(line))).toBe(true)
  })
})

describe('describeRule', () => {
  it('空槽显示未完成', () => {
    expect(describeRule(rule({ draft: true, time: '' }))).toBe('未完成')
  })

  it('每天带时刻', () => {
    expect(describeRule(rule({ time: '20:00' }))).toBe('每天 20:00')
  })
})

describe('describeCron · cron 反解人话', () => {
  it('旧展开表达式不反推补发配置，保留为自定义 cron', () => {
    expect(describeCron('continuous', '0,1,2,15,16,17,30,31,32,45,46,47 * * * *')).toBeNull()
  })

  it('每天带时间、无补发不加后缀', () => {
    expect(describeCron('continuous', buildCronList('continuous', ['d1'], 0, 0).join(' | '))).toBe('每天 08:00')
  })

  it('每小时 / 每 4 小时不误拼机器味 sub', () => {
    expect(describeCron('continuous', '0 * * * *')).toBe('每小时')
    expect(describeCron('continuous', '0 */4 * * *')).toBe('每 4 小时')
  })

  it('分钟乱序 / 重复仍能归一识别', () => {
    expect(describeCron('continuous', '45,0,30,15,15 * * * *')).toBe('每 15 分钟')
  })

  it('时段市场预设带交易日时间', () => {
    expect(describeCron('cn_stock', buildCronList('cn_stock', ['open'], 0, 0).join(' | '))).toBe('每交易日 · 开盘 09:30')
  })

  it('通用固定时刻使用自然语言', () => {
    expect(describeCron('continuous', '0 15 * * *')).toBe('每天 15:00')
  })

  it('按 APScheduler 编号描述显式星期', () => {
    expect(describeCron('continuous', '0 9 * * 0,2,4')).toBe('每周一、三、五 09:00')
    expect(describeCron('continuous', '0 9 * * 6')).toBe('每周日 09:00')
  })

  it('描述常见分钟和小时步长', () => {
    expect(describeCron('continuous', '*/15 * * * *')).toBe('每 15 分钟')
    expect(describeCron('continuous', '0 */4 * * *')).toBe('每 4 小时')
  })

  it('合并星期相同的多个固定时刻', () => {
    expect(describeCron('continuous', '0 9 * * * | 0 15 * * *')).toBe('每天 09:00、15:00')
  })

  it('不猜测复杂日期、范围或异构多规则', () => {
    expect(describeCron('continuous', '0 9 1 * *')).toBeNull()
    expect(describeCron('continuous', '0 9 * * 0-4')).toBeNull()
    expect(describeCron('continuous', '0 9 * * * | */15 * * * *')).toBeNull()
  })

  it('预设未命中时继续描述通用固定时刻', () => {
    expect(describeCron('continuous', '7 3 * * *')).toBe('每天 03:07')
    expect(describeCron('cn_stock', buildCronList('cn_stock', ['open', 'close'], 0, 0).join(' | '))).toBe(
      '每天 09:30、14:50',
    )
  })

  it('空表达式返回 null', () => {
    expect(describeCron('continuous', '')).toBeNull()
  })
})

describe('nextFires · APScheduler 星期语义', () => {
  it('0 表示周一而不是周日', () => {
    setSystemTime(new Date(2026, 7, 23, 12, 0))
    try {
      const [next] = nextFires(['0 9 * * 0'], 1)
      expect(next?.getDay()).toBe(1)
      expect(next?.getHours()).toBe(9)
    } finally {
      setSystemTime()
    }
  })
})

describe('parseTimerIntent · 编辑页回填', () => {
  it('空 cron → 关自动 + 默认快捷', () => {
    const s = parseTimerIntent('continuous', '')
    expect(s.autoOn).toBe(false)
    expect(s.timerTab).toBe('quick')
    expect(s.presetIds).toEqual(['d1'])
    expect(timerStateToCronExpr('continuous', s)).toBe('')
  })

  it('旧偏移 cron 保留为自定义表达式，不推断补发', () => {
    const s = parseTimerIntent('continuous', '0,1,2,15,16,17,30,31,32,45,46,47 * * * *')
    expect(s.autoOn).toBe(true)
    expect(s.timerTab).toBe('custom')
    expect(s.supN).toBe(0)
    expect(s.supM).toBe(1)
    expect(timerStateToCronExpr('continuous', s)).toBe('0,1,2,15,16,17,30,31,32,45,46,47 * * * *')
  })

  it('单一非预设日频 → 高级 d1 规则', () => {
    const s = parseTimerIntent('continuous', '0 21 * * *')
    expect(s.autoOn).toBe(true)
    expect(s.timerTab).toBe('advanced')
    expect(s.scheduleRules).toHaveLength(1)
    expect(s.scheduleRules![0]!.freq).toBe('d1')
    expect(s.scheduleRules![0]!.time).toBe('21:00')
  })

  it('多条日频 → 高级多规则', () => {
    const s = parseTimerIntent('continuous', '0 8 * * * | 0 20 * * *')
    expect(s.timerTab).toBe('advanced')
    expect(s.scheduleRules).toHaveLength(2)
    expect(s.scheduleRules!.map((r) => r.time).sort()).toEqual(['08:00', '20:00'])
  })

  it('无法结构化 → 自定义模式', () => {
    const s = parseTimerIntent('continuous', '7 3 * * * | */5 * * * *')
    expect(s.timerTab).toBe('custom')
    expect(s.rawCron).toContain('7 3 * * *')
    expect(timerStateToCronExpr('continuous', s)).toBe('7 3 * * * | */5 * * * *')
  })
})

describe('timerEditorError · 自定义模式', () => {
  const state = parseTimerIntent('continuous', '7 3 * * * | */5 * * * *')

  it('自动调仓开启时拒绝空内容与错误格式', () => {
    expect(timerEditorError({ ...state, rawCron: '' })).toBe('自定义节奏不能为空。')
    expect(timerEditorError({ ...state, rawCron: '7 3 * *' })).toBe(
      '自定义节奏格式有误：每条规则须包含 5 项（分、时、日、月、星期）。',
    )
  })

  it('内容有效或自动调仓关闭时不报错', () => {
    expect(timerEditorError(state)).toBeNull()
    expect(timerEditorError({ ...state, autoOn: false, rawCron: '' })).toBeNull()
  })
})


describe('独立补发配置', () => {
  it('旧展开表达式原样保留，不反推补发', () => {
    const expr = '0,1,2,15,16,17,30,31,32,45,46,47 * * * *'
    const state = parseSavedTimer('continuous', expr)
    expect(state.supN).toBe(0)
    expect(timerSupplement(state)).toBeNull()
    expect(timerStateToCronExpr('continuous', state)).toBe(expr)
  })
  it('基础 cron 与补发配置分别编译，零次存 null', () => {
    const state = parseSavedTimer('continuous', '*/15 * * * *', undefined, { count: 2, interval_minutes: 1 })
    expect(timerStateToCronExpr('continuous', state)).not.toContain('0,1,2,')
    expect(timerSupplement(state)).toEqual({ count: 2, interval_minutes: 1 })
    expect(timerSupplement({ ...state, supN: 0 })).toBeNull()
  })
})
