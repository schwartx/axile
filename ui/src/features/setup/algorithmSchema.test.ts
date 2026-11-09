import { describe, expect, it, test } from 'bun:test'

import {
  algorithmSchemaDefaults,
  algorithmSchemaFields,
  validateAlgorithmSchemaParams,
} from './algorithmSchema'

const schema: Record<string, unknown> = {
  type: 'object',
  properties: {
    pace: { type: 'number', title: '执行节奏', default: 0.5, exclusiveMinimum: 0, maximum: 1 },
    rounds: { type: 'integer', title: '轮次', default: 3, minimum: 1 },
    mode: { type: 'string', title: '模式', enum: ['steady', 'fast'], default: 'steady' },
    enabled: { type: 'boolean', title: '启用', default: true },
  },
}

const decimal = {
  anyOf: [{ type: 'number', exclusiveMinimum: 0 }, { type: 'string', pattern: '^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$' }],
  title: '开仓金额',
}
const decimalSchema = { type: 'object', properties: {
  amount: decimal,
  fee: { ...decimal, anyOf: [{ type: 'number', minimum: 0, maximum: 0 }, decimal.anyOf[1]], title: '费率' },
  price: { ...decimal, anyOf: [...decimal.anyOf, { type: 'null' }], default: null, title: '保本价' },
}, required: ['amount', 'fee'] }

describe('algorithmSchemaFields', () => {
  it('解析扁平 primitive 字段并提取默认值', () => {
    const fields = algorithmSchemaFields(schema)
    expect(fields?.map((field) => field.name)).toEqual(['pace', 'rounds', 'mode', 'enabled'])
    expect(algorithmSchemaDefaults(fields ?? [])).toEqual({ pace: 0.5, rounds: 3, mode: 'steady', enabled: true })
  })

  it('嵌套对象和数组不做不完整的结构化编辑', () => {
    expect(algorithmSchemaFields({ type: 'object', properties: { nested: { type: 'object' } } })).toBeNull()
    expect(algorithmSchemaFields({ type: 'object', properties: { values: { type: 'array' } } })).toBeNull()
  })

  it('解析 Pydantic Decimal 和可空参数，保留数值边界与空默认值', () => {
    const fields = algorithmSchemaFields(decimalSchema)!
    expect(fields.map((field) => field.name)).toEqual(['amount', 'fee', 'price'])
    expect(fields[0].schema).toMatchObject({ type: 'number', decimal: true, title: '开仓金额', exclusiveMinimum: 0 })
    expect(fields[2].schema.nullable).toBe(true)
    expect(algorithmSchemaDefaults(fields)).toEqual({ price: null })
    const nullable = algorithmSchemaFields({ type: 'object', properties: {
      rounds: { anyOf: [{ type: 'integer', minimum: 1 }, { type: 'null' }], title: '轮次', default: null },
    } })!
    expect(nullable[0].schema).toMatchObject({ type: 'integer', nullable: true, minimum: 1, title: '轮次' })
  })

  it('任意字符串联合类型仍回退 JSON，避免误当 Decimal', () => {
    expect(algorithmSchemaFields({ type: 'object', properties: {
      value: { anyOf: [{ type: 'number' }, { type: 'string' }] },
    } })).toBeNull()
  })
})

describe('validateAlgorithmSchemaParams', () => {
  it('校验类型、枚举和数值边界', () => {
    expect(validateAlgorithmSchemaParams({ pace: 0, rounds: 3 }, schema)).toContain('大于 0')
    expect(validateAlgorithmSchemaParams({ pace: 0.5, rounds: 1.5 }, schema)).toContain('整数')
    expect(validateAlgorithmSchemaParams({ mode: 'unknown' }, schema)).toContain('可选范围')
    expect(validateAlgorithmSchemaParams({ enabled: 'yes' }, schema)).toContain('布尔')
    expect(validateAlgorithmSchemaParams({ pace: 0.5, rounds: 3, mode: 'fast', enabled: false }, schema)).toBeNull()
  })

  it('Decimal 接受原有数值及精确字符串，可空价格不报错', () => {
    expect(validateAlgorithmSchemaParams({ amount: 6, fee: 0, price: null }, decimalSchema)).toBeNull()
    expect(validateAlgorithmSchemaParams({ amount: '6.123456789012345678901', fee: '0', price: '0.000000000000000000001' }, decimalSchema)).toBeNull()
    expect(validateAlgorithmSchemaParams({ fee: 0 }, decimalSchema)).toContain('开仓金额为必填')
    expect(validateAlgorithmSchemaParams({ amount: null, fee: 0 }, decimalSchema)).toContain('开仓金额必须是数字')
    for (const value of ['', ' ', 'NaN', 'Infinity', 'abc', '--1', false]) {
      expect(validateAlgorithmSchemaParams({ amount: value, fee: 0 }, decimalSchema)).toContain('必须是数字')
    }
    expect(validateAlgorithmSchemaParams({ amount: '0', fee: 0 }, decimalSchema)).toContain('大于 0')
    expect(validateAlgorithmSchemaParams({ amount: '6', fee: '0.000000000000000000001' }, decimalSchema)).toContain('不能大于 0')
  })

  it('Decimal 边界不按浮点精度舍入，支持负数和科学计数的边界', () => {
    const bounded = { type: 'object', properties: { amount: {
      ...decimal, anyOf: [{ type: 'number', minimum: -0.3, maximum: 1e-7 }, decimal.anyOf[1]],
    } } }
    expect(validateAlgorithmSchemaParams({ amount: '-0.300000000000000000001' }, bounded)).toContain('不能小于')
    expect(validateAlgorithmSchemaParams({ amount: '0.000000100000000000000001' }, bounded)).toContain('不能大于')
    expect(validateAlgorithmSchemaParams({ amount: '0.0000001' }, bounded)).toBeNull()
    expect(validateAlgorithmSchemaParams({ amount: '-.3' }, bounded)).toBeNull()
  })

  it('Decimal 步长也使用精确小数校验', () => {
    const stepped = { type: 'object', properties: { amount: {
      ...decimal, anyOf: [{ type: 'number', multipleOf: 0.1 }, decimal.anyOf[1]],
    } } }
    expect(validateAlgorithmSchemaParams({ amount: '0.3' }, stepped)).toBeNull()
    expect(validateAlgorithmSchemaParams({ amount: '0.300000000000000000001' }, stepped)).toContain('按 0.1 递增')
  })
})

// 显示扩展不改变持久化的小数参与率，也不引入前端算法文案。
test('服务端百分比与枚举显示元数据', async () => {
  const { algorithmNumericDisplay, algorithmFieldOptions } = await import('./algorithmSchema')
  const display = algorithmNumericDisplay({ type: 'number', exclusiveMinimum: 0, maximum: 1, 'x-display-scale': 100, 'x-display-step': 0.01, 'x-unit': '%' }, 0.1)
  expect(display.value).toBe(10)
  expect(display.max).toBe(100)
  expect(display.exclusiveMin).toBe(true)
  expect(12.5 / display.scale).toBe(0.125)
  expect(algorithmFieldOptions({ type: 'string', enum: ['ACTIVE'], 'x-enum-labels': { ACTIVE: '后端新名称' } })).toEqual([{ value: 'ACTIVE', label: '后端新名称' }])
})
