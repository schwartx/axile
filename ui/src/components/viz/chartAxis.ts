export function niceValueAxis(values: number[], emptySpan = 0.01) {
  const finite = values.filter(Number.isFinite)
  const low = Math.min(0, ...finite), high = Math.max(0, ...finite)
  const span = high - low || emptySpan
  const rough = span * 1.2 / 5
  const power = 10 ** Math.floor(Math.log10(rough))
  const step = ([1, 2, 2.5, 5, 10].find(n => n * power >= rough) ?? 10) * power
  const min = low === 0 ? 0 : Math.floor((low - span * 0.05) / step) * step
  const max = Math.ceil((high + span * 0.12) / step) * step
  const ticks = Array.from({ length: Math.round((max - min) / step) + 1 }, (_, i) => min + i * step)
  return { min, max, step, ticks }
}
