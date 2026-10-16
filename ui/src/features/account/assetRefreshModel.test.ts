import { expect, test } from 'bun:test'
import { ApiError } from '@/lib/api/client'
import { assetRefreshFeedback, isAssetRefreshBusy } from '@/features/account/assetRefreshModel'

test('自动刷新遇到忙碌保持安静，手动刷新保留中性反馈与原始错误', () => {
  const busy = new ApiError(409, '账户正在执行或刷新资产，请稍后再试')
  expect(assetRefreshFeedback(null, busy)).toEqual({ error: null, busy: false })
  expect(assetRefreshFeedback(busy, null)).toEqual({ error: busy, busy: true })
})

test('独立错误保留且由手动刷新结果覆盖自动刷新结果', () => {
  const failure = new ApiError(500, '账户正在执行或刷新资产，请稍后再试')
  expect(isAssetRefreshBusy(failure)).toBe(false)
  expect(isAssetRefreshBusy(new Error('账户正在执行或刷新资产，请稍后再试'))).toBe(false)
  expect(assetRefreshFeedback(null, failure).error).toBe(failure)
  const manual = new ApiError(422, 'invalid')
  expect(assetRefreshFeedback(manual, failure).error).toBe(manual)
})
