import { GlobalErrorContext } from '@/lib/errorPresentation'
import { ServiceConnectionStatus } from '@/components/ServiceConnectionStatus'
import { Outlet, useLocation } from 'react-router'
import { Link } from '@/components/ui/nav'
import { Toast } from '@/components/Toast'
import { ThemeToggle } from '@/components/ThemeToggle'
import { BrandWordmark } from '@/components/brand/BrandWordmark'
import { flowOf, ACCT_STEPS, type Step } from '@/features/setup/steps'

function Rail({ steps, index, title }: { steps: Step[]; index: number; title: string }) {
  return (
    <aside className="flex w-full flex-none overflow-x-auto border-b border-line bg-surface px-3 py-2 sm:block sm:w-[248px] sm:overflow-y-auto sm:border-r sm:border-b-0 sm:px-[18px] sm:py-7">
      <div className="hidden px-2.5 pb-3.5 text-xs font-semibold tracking-wide text-ink-3 sm:block">{title}</div>
      {steps.map((s, i) => {
        const done = i < index
        const cur = i === index
        return (
          <Link
            key={s.path}
            to={s.path}
            className={`flex min-w-fit flex-none items-center gap-2 rounded-[8px] px-3 py-2 text-[14px] sm:mb-1 sm:w-full sm:gap-3 sm:p-2.5 sm:text-[15px] ${
              cur ? 'bg-accent-soft font-semibold text-ink-1' : 'text-ink-2'
            }`}
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
            {s.label}
          </Link>
        )
      })}
    </aside>
  )
}

/** 向导外壳：独立全屏布局（头部 + 步骤栏 + 内容）。 */
export function WizardLayout() {
  const { pathname } = useLocation()
  const { flow, index } = flowOf(pathname)

  return (
    <GlobalErrorContext.Provider value={true}><div className="flex h-screen flex-col">
      <header className="flex min-h-14 flex-wrap flex-none items-center gap-3.5 border-b border-line bg-surface px-4 sm:px-6">
        <Link to="/" aria-label="axile 首页" className="text-ink-1">
          <BrandWordmark size={16.5} />
        </Link>
        <span className="text-[15px] text-ink-2">{flow === 'acct' ? '· 账户设置' : ''}</span>
        <ServiceConnectionStatus />
        <span className="ml-auto" />
        <ThemeToggle />
        <Link to="/" className="rounded-[9px] border border-line px-3.5 py-1.5 text-[14px] text-ink-2">
          关闭
        </Link>
      </header>

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden sm:flex-row">
        {flow === 'acct' && <Rail steps={ACCT_STEPS} index={index} title="账户设置" />}
        <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
          <Outlet />
        </div>
      </div>
      <Toast />
    </div></GlobalErrorContext.Provider>
  )
}
