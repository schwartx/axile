/** 背离预览条（纯 DOM，无图表库）：把「持仓 vs 目标」的差距画成一根有绝对刻度的鸟瞰条。 */
import { Tooltip } from '@/components/ui/Tooltip'
import type { RebalanceRow } from '@/lib/derive'

/** 满刻度：总换手达权益的此百分比即填满整条（全书重建量级）。 */
const FULL_SCALE = 100

/** 逐只动作标签：翻向属「注意」走琥珀，其余仓位调整均为中性过程步。 */
const ACTION_TAG: Record<RebalanceRow['action'], { text: string; cls: string }> = {
  aligned: { text: '到位', cls: 'text-ink-3' },
  open: { text: '建仓', cls: 'text-ink-3' },
  close: { text: '清仓', cls: 'text-ink-3' },
  increase: { text: '加仓', cls: 'text-ink-3' },
  reduce: { text: '减仓', cls: 'text-ink-3' },
  flip: { text: '翻向', cls: 'rounded bg-warn-tint px-1 text-warn' },
}

/** 带符号百分比，负号即空头/欠配方向，保留一位小数。 */
function pct(v: number): string {
  return `${v.toFixed(1)}%`
}

/** 单段悬浮卡：只讲这一只——品种·动作·当前→目标·买卖幅度与占换手比。 */
function SegDetail({ row, turnover }: { row: RebalanceRow; turnover: number }) {
  const tag = ACTION_TAG[row.action]
  const share = turnover > 0 ? (row.amount / turnover) * 100 : 0
  return (
    <div className="whitespace-nowrap">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-[13.5px] font-medium text-ink-1">{row.symbol}</span>
        <span className={`text-[12.5px] ${tag.cls}`}>{tag.text}</span>
      </div>
      <div className="num mt-1 text-ink-2">
        {pct(row.cur)} <span className="text-ink-3">→</span> {pct(row.tgt)}
      </div>
      <div className="num mt-1 text-[12.5px] text-ink-3">
        {row.side === 'buy' ? '买' : '卖'} {row.amount.toFixed(1)}% · 占换手 {share.toFixed(0)}%
      </div>
    </div>
  )
}

/**
 * 按各品种「要成交幅度」|Δ| 渲染换手条，条长带绝对刻度、逐段可悬起.
 *
 * 与当前分布条（``ExposureBar``）的区别：段宽取 ``|cur-tgt|`` 而非当前占比。
 * 关键口径：填充总宽 = ``min(FULL_SCALE, Σ|Δ|)``，即「本次调仓要动用的名义 ÷ 权益」，
 * 故条**越长＝总换手越大**（挪 8% 只填 8%，全书重建≈填满），留出的空槽即「未动用容量」。
 * 填充内部按各段 ``amount`` 相对细分，仍一眼看出谁占大头。
 * 颜色守 theme.css 铁律：drift 属「偏离」域，主体软琥珀（``warn-mid``），翻向靠深琥珀
 * （``warn``）与细斜纹单独顶出，不依赖颜色辨认。
 *
 * 交互：光标压到哪段，哪段就从扁条里**向上长起**（``items-end`` 使抬升朝上）、点亮为实琥珀
 * 并投影浮出，其余段 ``group-has`` 压暗做 focus+context；该段头顶弹带箭头的通用悬浮卡，只讲
 * 这一只（与明细抽屉同口径，均出自 ``rebalancePlan``）。段可 Tab 聚焦，键盘同样可唤出卡。
 */
export function DriftBar({ rows }: { rows: RebalanceRow[] }) {
  const segs = rows.filter((r) => r.action !== 'aligned')
  if (segs.length === 0) return null
  const turnover = segs.reduce((s, r) => s + r.amount, 0)
  const fillPct = Math.min(FULL_SCALE, turnover)
  const inner = turnover || 1
  return (
    <div className="mt-2 pt-2.5">
      <div className="h-2 w-full rounded-full bg-line">
        <div className="group/fill flex h-full w-full items-end" style={{ width: `${fillPct.toFixed(2)}%` }}>
          {segs.map((r) => (
            <Tooltip key={r.symbol} content={<SegDetail row={r} turnover={turnover} />} arrow>
              <span
                tabIndex={0}
                aria-label={`${r.symbol} ${ACTION_TAG[r.action].text} ${pct(r.cur)} 到 ${pct(r.tgt)}`}
                style={{ width: `${((r.amount / inner) * 100).toFixed(2)}%` }}
                className={`block h-2 cursor-help outline-none transition-[height,background-color,opacity,box-shadow] duration-150 ease-out first:rounded-l-full last:rounded-r-full [&+&]:border-l [&+&]:border-surface ${
                  r.action === 'flip' ? 'chart-hatch bg-warn' : 'bg-warn-mid'
                } group-has-[:hover]/fill:opacity-40 group-has-[:focus-visible]/fill:opacity-40 hover:!opacity-100 hover:z-10 hover:h-[13px] hover:rounded-full hover:bg-warn hover:shadow-[0_4px_10px_rgba(0,0,0,0.28)] focus-visible:!opacity-100 focus-visible:z-10 focus-visible:h-[13px] focus-visible:rounded-full focus-visible:bg-warn focus-visible:shadow-[0_4px_10px_rgba(0,0,0,0.28)]`}
              />
            </Tooltip>
          ))}
        </div>
      </div>
    </div>
  )
}
