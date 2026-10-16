import { describe, it, expect } from 'bun:test'
import { algorithmRefOf, validateAlgorithmRef, validateAlgorithmParams, describeAlgorithmRef, registerAlgorithmLabels } from './algorithms'

describe('algorithmRefOf · 账户 JSON 安全还原', () => {
  it('还原合法引用，params 缺省补空对象', () => {
    expect(algorithmRefOf({ method: 'TWAP', params: { slices: 10 } })).toEqual({
      method: 'TWAP',
      params: { slices: 10 },
    })
    expect(algorithmRefOf({ method: 'POV' })).toEqual({ method: 'POV', params: {} })
  })

  it('结构不符一律返回 null', () => {
    expect(algorithmRefOf(null)).toBeNull()
    expect(algorithmRefOf(undefined)).toBeNull()
    expect(algorithmRefOf('TWAP')).toBeNull()
    expect(algorithmRefOf({ params: {} })).toBeNull()
    expect(algorithmRefOf({ method: 42 })).toBeNull()
  })
})

describe('validateAlgorithmParams', () => {
  it('拒绝 max_wait_seconds=0（越界）', () => {
    expect(validateAlgorithmParams({ max_wait_seconds: 0 })).toContain('max_wait_seconds')
  })

  it('拒绝启用追单时 max_chase_count 超上限', () => {
    const err = validateAlgorithmParams({ chase_enabled: true, max_chase_count: 99, chase_interval: 5 })
    expect(err).toContain('max_chase_count')
  })

  it('拒绝追单总时长超过 600s', () => {
    const err = validateAlgorithmParams({ chase_enabled: true, max_chase_count: 40, chase_interval: 20 })
    expect(err).toContain('600')
  })

  it('合法参数返回 null', () => {
    expect(
      validateAlgorithmParams({ max_wait_seconds: 3600, chase_enabled: true, max_chase_count: 50, chase_interval: 5 }),
    ).toBeNull()
  })

  it('未启用追单时不校验追单族', () => {
    expect(validateAlgorithmParams({ chase_enabled: false, max_chase_count: 999 })).toBeNull()
  })

  it('拒绝非法枚举、非整数追单次数和非数字等待时间', () => {
    expect(validateAlgorithmParams({ price_strategy: 'MARKET' })).toContain('price_strategy')
    expect(validateAlgorithmParams({ close_order_type: 'INVALID' }, 'TARGET-POS-TASK')).toContain('close_order_type')
    expect(validateAlgorithmParams({ open_order_type: 'INVALID' }, 'TARGET-POS-TASK')).toContain('open_order_type')
    expect(validateAlgorithmParams({ close_order_type: 'MARKET', open_order_type: 'LIMIT' }, 'TARGET-POS-TASK')).toBeNull()
    expect(validateAlgorithmParams({ on_missing_book: 'fallback' })).toContain('on_missing_book')
    expect(validateAlgorithmParams({ max_wait_seconds: '60' })).toContain('必须是数字')
    expect(validateAlgorithmParams({ chase_enabled: true, max_chase_count: 1.5 })).toContain('整数')
  })

  it('按算法拒绝 TWAP 过密切片和 POV 无法完成一次轮询', () => {
    expect(validateAlgorithmRef({ method: 'TWAP', params: { total_duration: 1, slices: 1000 } })).toContain('0.1s')
    expect(validateAlgorithmRef({ method: 'POV', params: { interval_seconds: 10, max_duration: 1 } })).toContain('max_duration')
  })

  it('校验 POV 参与率与布尔参数', () => {
    expect(validateAlgorithmRef({ method: 'POV', params: { participation_rate: 0 } })).toContain('(0, 1]')
    expect(validateAlgorithmRef({ method: 'POV', params: { complete_on_timeout: 'yes' } })).toContain('布尔')
  })
})


it('算法摘要不再反推意图，采用后端名称', () => {
  registerAlgorithmLabels([{ name: 'SINGLE-MAKER', label: '后端挂单名', description: '', channels: null, slots: null, builtin: true, default_params: {}, params_schema: {} }])
  expect(describeAlgorithmRef({ method: 'SINGLE-MAKER', params: { price_strategy: 'PASSIVE', chase_enabled: true, max_wait_seconds: 60 } })).toBe('后端挂单名')
  registerAlgorithmLabels([])
  expect(describeAlgorithmRef({ method: 'SINGLE-MAKER', params: {} })).toBe('SINGLE-MAKER')
})

it('多字段校验优先采用服务端默认值', () => {
  registerAlgorithmLabels([{ name: 'TWAP', label: 'TWAP', description: '', channels: null, slots: null, builtin: true,
    default_params: { total_duration: 1, slices: 10 }, params_schema: {} }])
  expect(validateAlgorithmRef({ method: 'TWAP', params: { slices: 20 } })).toContain('0.1s')
  registerAlgorithmLabels([])
})
