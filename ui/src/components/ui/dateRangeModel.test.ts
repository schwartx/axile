import { describe, expect, it } from 'bun:test'
import { pickRange, presetRange, rangeDays } from './dateRangeModel'

describe('date range selection', () => {
  it('starts a fresh range and only completes on the second click', () => {
    const first = pickRange({ from: '2026-09-01', to: '2026-09-30' }, null, '2026-09-16', 31)
    expect(first).toEqual({ value: { from: '2026-09-16', to: '' }, anchor: '2026-09-16' })
    expect(pickRange(first.value, first.anchor, '2026-10-15', 31)).toEqual({ value: { from: '2026-09-16', to: '2026-10-15' }, anchor: null })
  })
  it('allows the same day and inclusive 31 days, rejecting 32 without truncation', () => {
    const value = { from: '2026-09-16', to: '' }
    expect(pickRange(value, value.from, '2026-09-16', 31).value.to).toBe('2026-09-16')
    expect(pickRange(value, value.from, '2026-10-16', 31).anchor).toBeNull()
    expect(pickRange(value, value.from, '2026-10-17', 31)).toEqual({ value, anchor: value.from })
  })
  it('clicking earlier restarts the start date', () => {
    expect(pickRange({ from: '2026-09-16', to: '' }, '2026-09-16', '2026-09-14', 31)).toEqual({ value: { from: '2026-09-14', to: '' }, anchor: '2026-09-14' })
  })
  it('counts real inclusive days and leaves invalid or incomplete ranges unknown', () => {
    expect(rangeDays({ from: '2024-02-28', to: '2024-03-01' })).toBe(3)
    expect(rangeDays({ from: '2026-07-01', to: '2026-09-28' })).toBe(90)
    for (const value of [{ from: '', to: '' }, { from: '2026-09-02', to: '2026-09-01' }, { from: '2026-02-30', to: '2026-03-01' }]) expect(rangeDays(value)).toBeNull()
  })
  it('quick ranges cross month/year boundaries without local timezone arithmetic', () => {
    expect(presetRange('2026-01-01', 1)).toEqual({ from: '2026-01-01', to: '2026-01-01' })
    expect(presetRange('2026-01-01', 1, -1)).toEqual({ from: '2025-12-31', to: '2025-12-31' })
    expect(presetRange('2026-01-01', 7)).toEqual({ from: '2025-12-26', to: '2026-01-01' })
    expect(rangeDays(presetRange('2026-01-01', 30))).toBe(30)
  })
})
