import { describe, expect, it } from 'bun:test'
import { calendarKeyDay, clampDay, monthDays, monthWeeks, moveDay, moveMonth, parseDay } from './datePickerModel'

describe('theme calendar dates', () => {
  it('rejects overflowing dates and accepts valid leap days', () => {
    expect(parseDay('2026-02-29')).toBeNull()
    expect(parseDay('2026-02-30')).toBeNull()
    expect(parseDay('2026-9-01')).toBeNull()
    expect(parseDay('2024-02-29')).not.toBeNull()
    expect(parseDay('')).toBeNull()
  })
  it('renders a Monday-first six-week grid with neighbouring days', () => {
    const days = monthDays('2026-09')
    expect(days).toHaveLength(42)
    expect(days[0]).toBe('2026-08-31')
    expect(days.at(-1)).toBe('2026-10-11')
    expect(days.filter(day => day.startsWith('2026-09'))).toHaveLength(30)
  })
  it('uses only the weeks containing dates, including genuine six-week months', () => {
    expect(monthWeeks('2026-09')).toBe(5)
    expect(monthWeeks('2026-10')).toBe(5)
    expect(monthWeeks('2026-03')).toBe(6)
    expect(monthWeeks('2027-02')).toBe(4)
  })
  it('crosses year and leap-month boundaries without local-time drift', () => {
    expect(moveDay('2026-12-31', 1)).toBe('2027-01-01')
    expect(moveMonth('2024-01-31', 1)).toBe('2024-02-29')
    expect(moveMonth('2026-01-31', 1)).toBe('2026-02-28')
    expect(moveMonth('2026-01-31', -1)).toBe('2025-12-31')
  })
  it('keeps keyboard navigation within valid start/end bounds', () => {
    expect(clampDay('2026-09-15', '2026-09-16', '2026-10-15')).toBe('2026-09-16')
    expect(clampDay('2026-10-16', '2026-09-16', '2026-10-15')).toBe('2026-10-15')
    expect(clampDay('2026-10-01', 'invalid', '')).toBe('2026-10-01')
    expect(calendarKeyDay('2026-09-16', 'Home')).toBe('2026-09-14')
    expect(calendarKeyDay('2026-09-16', 'End')).toBe('2026-09-20')
    expect(calendarKeyDay('2026-09-16', 'ArrowUp')).toBe('2026-09-09')
    expect(calendarKeyDay('2026-09-16', 'PageDown')).toBe('2026-10-16')
    expect(calendarKeyDay('2026-09-16', 'Enter')).toBeNull()
  })
})
