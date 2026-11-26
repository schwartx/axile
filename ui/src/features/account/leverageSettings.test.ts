import { expect, test } from 'bun:test'

import { leverageSettingsPatch } from './leverageSettings'

const baseline = { long_leverage: 1, short_leverage: 1, weight_precision: 0.01 }

test('杠杆设置只提交改过的字段，保留显式的零杠杆', () => {
  expect(leverageSettingsPatch(baseline, baseline)).toEqual({})
  expect(leverageSettingsPatch(baseline, { ...baseline, long_leverage: 0, weight_precision: 0.001 }))
    .toEqual({ long_leverage: 0, weight_precision: 0.001 })
})

test('未展示的空头杠杆不进入 PATCH', () => {
  expect(leverageSettingsPatch(baseline, { ...baseline, short_leverage: null })).toEqual({})
})
