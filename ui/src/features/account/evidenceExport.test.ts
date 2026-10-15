import { describe, expect, it } from 'bun:test'
import { evidenceDateError, evidenceDatesFromSnapshot, evidenceDatesFromWindow, evidenceDay, evidenceFilename, evidenceUrl, type EvidenceOptions } from './evidenceExport'

const options: EvidenceOptions = { profile: 'shareable', includeLogs: false, dates: { from: '2026-09-01', to: '2026-09-30' } }

describe('evidence date scope', () => {
  it('uses Shanghai days and preserves a half-open 90-day window without truncating it', () => {
    expect(evidenceDay(Date.parse('2026-09-01T16:00:00Z'))).toBe('2026-09-02')
    const dates = evidenceDatesFromWindow({ start: Date.parse('2026-07-01T00:00:00+08:00'), end: Date.parse('2026-09-29T00:00:00+08:00') })
    expect(dates).toEqual({ from: '2026-07-01', to: '2026-09-28' })
    expect(evidenceDateError(dates)).toContain('31 天')
    expect(evidenceDatesFromWindow(null)).toEqual({ from: '', to: '' })
    expect(evidenceDay(NaN)).toBe('')
  })

  it('accepts inclusive 31 days and leap days, rejects 32 days, invalid and reversed dates', () => {
    expect(evidenceDateError({ from: '2026-01-01', to: '2026-01-31' })).toBeNull()
    expect(evidenceDateError({ from: '2026-01-01', to: '2026-02-01' })).toContain('31 天')
    expect(evidenceDateError({ from: '2024-02-29', to: '2024-02-29' })).toBeNull()
    expect(evidenceDateError({ from: '2026-02-29', to: '2026-03-01' })).toContain('有效日期')
    expect(evidenceDateError({ from: '2026-02-30', to: '2026-03-01' })).toContain('有效日期')
    expect(evidenceDateError({ from: '2026-01-02', to: '2026-01-01' })).toContain('早于')
    expect(evidenceDateError({ from: '', to: '2026-01-01' })).toContain('请选择')
  })

  it('maps snapshot (start, end] exactly and asks for dates when snapshot scope is unknown', () => {
    expect(evidenceDatesFromSnapshot({ day: '2026-09-01' })).toEqual({ from: '2026-09-01', to: '2026-09-01' })
    expect(evidenceDatesFromSnapshot({ start: Date.parse('2026-09-01T23:59:59.999+08:00'), end: Date.parse('2026-09-03T00:00:00+08:00') })).toEqual({ from: '2026-09-02', to: '2026-09-03' })
    expect(evidenceDatesFromSnapshot({})).toEqual({ from: '', to: '' })
  })
})

describe('evidence request and filename', () => {
  it('exports whole executions, with no page filter or snapshot selection in the query', () => {
    const url = new URL(evidenceUrl({ kind: 'account', accountId: 7 }, options), 'http://localhost')
    expect(url.pathname).toBe('/account/7/evidence')
    expect(Object.fromEntries(url.searchParams)).toEqual({ profile: 'shareable', include_logs: 'false', start_date: '2026-09-01', end_date: '2026-09-30', timezone: 'Asia/Shanghai' })
    expect(evidenceUrl({ kind: 'execution', accountId: 7, executionId: 'A/B ?' }, options)).toBe('/account/executions/A%2FB%20%3F/evidence?profile=shareable&include_logs=false')
  })

  it('uses generic names and sanitizes execution IDs; marks internal diagnostics', () => {
    expect(evidenceFilename({ kind: 'account', accountId: 7 }, options)).toBe('axile-evidence-2026-09-01_2026-09-30.zip')
    expect(evidenceFilename({ kind: 'execution', accountId: 7, executionId: '../../A' }, { ...options, profile: 'internal' })).toBe('axile-evidence-execution-______A-internal.zip')
  })
})
