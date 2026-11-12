import { PHASES, phaseIndex } from '@/features/dashboard/execProgress'

/**
 * 执行阶段条 —— 五步确定态进度：触发 → 冻结输入 → 算目标 → 下单 → 对账。
 *
 * 运行态走 accent（蓝，与状态 `run` 档一致，不碰红绿）。已过阶段实心、当前阶段
 * 发光脉冲、未到阶段留边。数据源为服务端 phase（SSE 热增量 / 轮询兜底），故进度真实
 * 反映后端在跑到哪一步，而非本地猜测。
 *
 * Parameters
 * ----------
 * phase : string
 *     当前阶段键（见 execProgress.PHASES）。
 */
export function PhaseBar({ phase, running = true, stale = false }: { phase: string | null; running?: boolean; stale?: boolean }) {
  const cur = phaseIndex(phase)
  return (
    <div role="group" aria-label="执行阶段" className="mt-3 flex items-center">
      {PHASES.map((p, i) => {
        const done = i < cur
        const active = i === cur
        return (
          <div key={p.key} className="flex flex-1 flex-col items-center gap-1.5 first:items-start last:items-end">
            <div className="flex w-full items-center">
              {/* 左连接线（首列无） */}
              {i > 0 && <div className={`h-0.5 flex-1 ${i <= cur ? 'bg-accent' : 'bg-line'}`} />}
              <div
                aria-current={active ? 'step' : undefined}
                aria-label={p.label}
                className={`h-2.5 w-2.5 flex-none rounded-full ${
                  done
                    ? 'bg-accent'
                    : active
                      ? `bg-accent ${running && !stale ? 'shadow-[0_0_0_3px_var(--color-accent-soft)] motion-safe:animate-pulse' : ''}`
                      : 'border-2 border-border-strong bg-surface'
                }`}
              />
              {/* 右连接线（末列无） */}
              {i < PHASES.length - 1 && <div className={`h-0.5 flex-1 ${i < cur ? 'bg-accent' : 'bg-line'}`} />}
            </div>
            <span className={`text-[12px] ${active ? 'font-semibold text-accent' : done ? 'text-ink-2' : 'text-ink-3'}`}>
              {p.label}
            </span>
          </div>
        )
      })}
    </div>
  )
}
