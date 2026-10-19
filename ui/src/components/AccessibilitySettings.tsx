import { useId } from 'react'
import { CenteredModal } from '@/components/ui/CenteredModal'
import { ChoiceGroup, type ChoiceOption } from '@/components/ui/ChoiceGroup'
import { AccessibilitySwitch } from '@/components/ui/AccessibilitySwitch'
import { useAccessibilityStore, type MarketPalette } from '@/stores/accessibility'

function PalettePreview({ up, down }: { up: string; down: string }) {
  return <span className="flex gap-3 font-mono text-[12px] font-semibold sm:text-sm">
    <span style={{ color: `var(--color-market-${up})` }}>+2.35%</span>
    <span style={{ color: `var(--color-market-${down})` }}>−1.28%</span>
  </span>
}

const PALETTES: ChoiceOption<MarketPalette>[] = [
  { value: 'red-up', label: '红涨绿跌', preview: <PalettePreview up="red" down="green" /> },
  { value: 'green-up', label: '绿涨红跌', preview: <PalettePreview up="green" down="red" /> },
  { value: 'blue-up', label: '蓝涨橙跌', preview: <PalettePreview up="blue" down="orange" /> },
  { value: 'purple-up', label: '紫涨绿跌', preview: <PalettePreview up="purple" down="green" /> },
  { value: 'red-cyan', label: '红涨青跌', preview: <PalettePreview up="red" down="cyan" /> },
  { value: 'monochrome', label: '单色', preview: <PalettePreview up="mono" down="mono" /> },
]

/** 纯静态样例，不请求行情，也不提供可编辑的示例输入。 */
function DisplayPreview() {
  const patternId = useId()
  return <section aria-label="实时预览" className="min-w-0 border-t border-line pt-5 md:border-t-0 md:border-l md:pt-0 md:pl-6">
    <h3 className="text-[15px] font-semibold">实时预览</h3>
    <p className="mt-5 text-sm text-ink-1">主文字 · 账户权益</p>
    <p className="mt-1 text-xs text-ink-2">次要文字 · 持仓与收益概览</p>
    <p className="mt-1 text-xs text-ink-3">辅助文字 · 仅用于显示效果预览</p>
    <div className="mt-5 flex flex-wrap items-center gap-3 text-sm">
      <span className="rounded-lg border border-ink-3/30 contrast:border-border-strong bg-surface px-3 py-1.5 text-ink-2">输入边界示例</span>
      <span className="rounded-lg border border-accent bg-accent-soft px-3 py-1.5 text-accent">✓ 已选中</span>
      <span className="text-accent">● 运行中</span>
      <span className="rounded bg-warn-soft px-2 py-1 text-warn">△ 警告</span>
    </div>
    <div className="mt-6 flex flex-wrap gap-4 font-mono text-sm font-semibold">
      <span className="text-up">+2.35%</span><span className="text-down">−1.28%</span><span className="text-ink-2">0.00%</span>
    </div>
    <svg role="img" aria-label="收益曲线、正负方向条形预览" viewBox="0 0 360 64" className="mt-5 block w-full">
      <defs>
        <pattern id={patternId} width="6" height="6" patternUnits="userSpaceOnUse">
          <path d="M-1 1L1-1M0 6L6 0M5 7L7 5" stroke="var(--color-surface)" strokeWidth="1.5" />
        </pattern>
      </defs>
      <path d="M0 54H360" stroke="var(--color-line)" />
      <path d="M4 40L42 33L80 36L118 18L156 24L194 8" fill="none" stroke="var(--color-up)" strokeWidth="2" />
      <path className="market-down-line" d="M4 12L42 18L80 15L118 32L156 28L194 44" fill="none" stroke="var(--color-down)" strokeWidth="2" />
      <path d="M224 54V28H250V54Z" fill="var(--color-up)" />
      <path d="M270 54V40H296V54Z" fill="var(--color-down)" />
      <path className="market-down-pattern" d="M270 54V40H296V54Z" fill={`url(#${patternId})`} />
    </svg>
  </section>
}

export function AccessibilitySettings({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { enhancedContrast, marketPalette, directionMarkers, storageAvailable, update } = useAccessibilityStore()
  const id = useId()
  const monochrome = marketPalette === 'monochrome'
  return <CenteredModal open={open} title="无障碍设置" onClose={onClose} widthClassName="w-[820px]"
    subtitle={<span role="status">{storageAvailable ? '即时生效，仅保存在当前浏览器' : '当前浏览器无法保存，刷新后恢复默认'}</span>}>
    <div className="grid gap-6 pb-4 md:grid-cols-[360px_minmax(0,1fr)]">
      <div className="min-w-0 space-y-5">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h3 id={`${id}-contrast`} className="text-[15px] font-semibold">增强对比度</h3>
            <p id={`${id}-description`} className="mt-1 text-sm text-ink-2">让次要文字、边框和图表更清楚</p>
          </div>
          <AccessibilitySwitch checked={enhancedContrast} labelledBy={`${id}-contrast`} describedBy={`${id}-description`}
            onChange={(checked) => update({ enhancedContrast: checked })} />
        </div>
        <section>
          <h3 className="mb-2 text-[15px] font-semibold">涨跌配色</h3>
          <ChoiceGroup value={marketPalette} options={PALETTES} onChange={(palette) => update({ marketPalette: palette })}
            variant="list" ariaLabel="涨跌配色" describedBy={`${id}-palette-note`} />
          <p id={`${id}-palette-note`} className="mt-2 text-xs text-ink-2">应用于涨跌、盈亏、多空及净敞口</p>
        </section>
        <div className="flex items-center justify-between gap-4">
          <div>
            <h3 id={`${id}-markers`} className="text-[15px] font-semibold">强化方向标记</h3>
            <p id={`${id}-markers-description`} className="mt-1 text-sm text-ink-2">
              用符号、线型和纹理辅助区分涨跌{monochrome ? '，单色方案始终开启' : ''}
            </p>
          </div>
          <AccessibilitySwitch checked={monochrome || directionMarkers} disabled={monochrome}
            labelledBy={`${id}-markers`} describedBy={`${id}-markers-description`}
            onChange={(checked) => update({ directionMarkers: checked })} />
        </div>
      </div>
      <DisplayPreview />
    </div>
  </CenteredModal>
}
