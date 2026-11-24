export interface ValueRange { min: number; max: number }

function tickStep(rough: number) {
  const power = 10 ** Math.floor(Math.log10(rough))
  return ([1, 2, 2.5, 5, 10].find(n => n * power >= rough) ?? 10) * power
}

export function rangedValueAxis({ min, max }: ValueRange) {
  const step = tickStep((max - min) / 5)
  const first = Math.ceil(min / step) * step
  const ticks = Array.from({ length: Math.max(0, Math.floor((max - first) / step) + 1) }, (_, i) => first + i * step)
  return { min, max, step, ticks }
}

export function scaleValueRange({ min, max }: ValueRange, factor: number): ValueRange {
  const center = (min + max) / 2
  const half = Math.max(1e-8, Math.min(1e8, (max - min) * factor / 2))
  return { min: center - half, max: center + half }
}

export function niceValueAxis(values: number[], emptySpan = 0.01) {
  const finite = values.filter(Number.isFinite)
  const low = Math.min(0, ...finite), high = Math.max(0, ...finite)
  const span = high - low || emptySpan
  const step = tickStep(span * 1.2 / 5)
  const min = low === 0 ? 0 : Math.floor((low - span * 0.05) / step) * step
  const max = Math.ceil((high + span * 0.12) / step) * step
  const ticks = Array.from({ length: Math.round((max - min) / step) + 1 }, (_, i) => min + i * step)
  return { min, max, step, ticks }
}
