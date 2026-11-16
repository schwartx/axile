import { replayHeatCells } from '../src/features/replay/chartGeometry'
import { heatResult, type HeatSource, type HeatQuery } from '../src/features/replay/heatData'
import { useMemo } from 'react'
export function useReplayHeat(sources: HeatSource[], query: HeatQuery | null) {
  const frame = useMemo(() => {
    if (!query) return null
    const cells = sources.flatMap(source => replayHeatCells(source.quotes, query.range, source.end, query.view, query.layout, source.gaps).cells)
    const data = new Float64Array(cells.flatMap(cell => [cell.x, cell.y, cell.quantity ?? NaN, Number(cell.unknown), cell.minPrice, cell.maxPrice]))
    return { query, value: heatResult(data, query) }
  }, [sources, query])
  return { frame, pending: false, error: null }
}
