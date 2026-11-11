import { OptionStepper } from '@/components/ui/OptionStepper'
import { MOTION_LAYOUT } from '@/lib/viewTransition'

const INTERVAL_MINUTES = Array.from({ length: 60 }, (_, index) => index + 1)

interface SupplementControlsProps {
  supN: number
  supM: number
  onN: (count: number) => void
  onM: (minutes: number) => void
}

/** 编辑页、向导与快捷弹层共用补发配置；关闭时保留间隔草稿。 */
export function SupplementControls({ supN, supM, onN, onM }: SupplementControlsProps) {
  const enabled = supN > 0
  return (
    <div role="group" aria-label="补发设置" className="inline-flex max-w-full items-center rounded-[9px] border border-line bg-surface px-1.5 text-sm focus-within:border-accent">
      <span className="shrink-0 pl-1 text-ink-3">补发</span>
      <OptionStepper appearance="inline" ariaLabel="补发次数" value={supN} options={[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]} onChange={onN}
        unit="次" zeroLabel="不补" />
      <div inert={!enabled} aria-hidden={!enabled} className={`grid transition-[grid-template-columns] ${MOTION_LAYOUT} ${enabled ? 'grid-cols-[1fr]' : 'grid-cols-[0fr]'}`}>
        <div className="min-w-0 overflow-hidden">
          <div className="flex w-max items-center">
            <span className="shrink-0 text-ink-3">，每隔</span>
            <OptionStepper appearance="inline" ariaLabel="补发间隔分钟" value={supM} options={INTERVAL_MINUTES} onChange={onM}
              unit="分" disabled={!enabled} />
          </div>
        </div>
      </div>
    </div>
  )
}
