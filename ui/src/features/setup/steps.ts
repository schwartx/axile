/** 向导步骤定义（路径 + 标签），供步骤栏与导航共用。 */

export interface Step {
  path: string
  label: string
}

export const ACCT_STEPS: Step[] = [
  { path: '/setup/acct/channel', label: '选渠道' },
  { path: '/setup/acct/connect', label: '连接' },
  { path: '/setup/acct/portfolio', label: '绑定组合' },
  { path: '/setup/acct/trade', label: '交易方式' },
  { path: '/setup/acct/timer', label: '定时' },
  { path: '/setup/acct/confirm', label: '确认' },
]

export const SIGNAL_STEPS: Step[] = [
  ACCT_STEPS[0],
  { path: '/setup/acct/signal', label: '账户与组合' },
  { path: '/setup/acct/signal?step=3', label: '设置行情' },
  { path: '/setup/acct/signal?step=4', label: '资金与持仓' },
  { path: '/setup/acct/signal?step=5', label: '定时' },
  { path: '/setup/acct/signal?step=6', label: '通知' },
  { path: '/setup/acct/signal?step=7', label: '试算与确认' },
]

/** 由当前路径推断所属流程与步骤序号。组合新建不走向导（列表页弹层 → 编辑器）。 */
export function flowOf(pathname: string, search = '', signalSelected = false): { flow: 'acct' | null; steps: Step[]; index: number } {
  if (pathname === SIGNAL_STEPS[1].path) {
    const index = SIGNAL_STEPS.findIndex((step) => step.path === pathname + search)
    return { flow: 'acct', steps: SIGNAL_STEPS, index: index < 1 ? 1 : index }
  }
  if (signalSelected && pathname === ACCT_STEPS[0].path) {
    return { flow: 'acct', steps: SIGNAL_STEPS, index: 0 }
  }
  if (pathname.startsWith('/setup/acct/')) {
    const index = ACCT_STEPS.findIndex((s) => s.path === pathname)
    return { flow: 'acct', steps: ACCT_STEPS, index }
  }
  return { flow: null, steps: [], index: -1 }
}
