export interface AlgorithmParamSchema {
  type: 'string' | 'integer' | 'number' | 'boolean'
  title?: string
  description?: string
  default?: unknown
  enum?: unknown[]
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number
  exclusiveMaximum?: number
  multipleOf?: number
  pattern?: string
  nullable?: boolean
  decimal?: boolean
  'x-enum-labels'?: Record<string, string>
  'x-control'?: 'choice' | 'cards' | 'numberflow' | 'stepper' | 'presets' | 'slider'
  'x-option-descriptions'?: Record<string, string>
  'x-presets'?: number[]
  'x-slider-min'?: number
  'x-slider-max'?: number
  'x-order'?: number
  'x-unit'?: string
  'x-display-scale'?: number
  'x-display-step'?: number
}

export interface AlgorithmSchemaField {
  name: string
  schema: AlgorithmParamSchema
  required: boolean
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function parseParamSchema(value: unknown): AlgorithmParamSchema | null {
  if (!isRecord(value)) return null
  if (Array.isArray(value.anyOf)) {
    const variants = value.anyOf.filter((item) => !isRecord(item) || item.type !== 'null')
    const nullable = variants.length !== value.anyOf.length
    let parsed: AlgorithmParamSchema | null = null
    if (variants.length === 1) parsed = parseParamSchema(variants[0])
    if (variants.length === 2) {
      const number = variants.find((item) => isRecord(item) && item.type === 'number')
      const string = variants.find((item) => isRecord(item) && item.type === 'string')
      // shortcut: 仅识别 Pydantic Decimal 的标准模式，其他联合类型保留 JSON 编辑。
      if (isRecord(number) && isRecord(string) && string.pattern === '^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$') {
        parsed = { ...number, pattern: string.pattern, type: 'number', decimal: true }
      }
    }
    if (!parsed || (nullable && parsed.enum)) return null
    return { ...parsed, ...value, type: parsed.type, nullable: nullable || parsed.nullable, decimal: parsed.decimal }
  }
  if (!['string', 'integer', 'number', 'boolean'].includes(String(value.type))) return null
  if (value.enum !== undefined && !Array.isArray(value.enum)) return null
  return value as unknown as AlgorithmParamSchema
}

/** 解析 Pydantic 的基础字段、Decimal 及可空字段；复杂结构保留 JSON 编辑。 */
export function algorithmSchemaFields(schema: Record<string, unknown>): AlgorithmSchemaField[] | null {
  if (schema.type !== 'object' || !isRecord(schema.properties)) return null
  const required = new Set(Array.isArray(schema.required) ? schema.required.filter((key): key is string => typeof key === 'string') : [])
  const fields: AlgorithmSchemaField[] = []
  for (const [name, raw] of Object.entries(schema.properties)) {
    const parsed = parseParamSchema(raw)
    if (!parsed) return null
    fields.push({ name, schema: parsed, required: required.has(name) })
  }
  return fields.sort((a, b) => (a.schema['x-order'] ?? 100) - (b.schema['x-order'] ?? 100))
}

/** 从 schema 提取字段默认值，供运行时 default_params 缺失时补齐。 */
export function algorithmSchemaDefaults(fields: AlgorithmSchemaField[]): Record<string, unknown> {
  return Object.fromEntries(
    fields.filter((field) => field.schema.default !== undefined).map((field) => [field.name, field.schema.default]),
  )
}

/** Decimal 字符串按相同小数位对齐成整数，避免校验时吞掉最后几位小数。 */
function decimalOperands(value: string | number, bound: number): [bigint, bigint] {
  const parts = (item: string | number) => {
    const [mantissa, exponent = '0'] = String(item).trim().split(/[eE]/)
    return { units: BigInt(mantissa.replace('.', '')), scale: (mantissa.split('.')[1]?.length ?? 0) - Number(exponent) }
  }
  const left = parts(value)
  const right = parts(bound)
  const scale = Math.max(left.scale, right.scale)
  return [left.units * 10n ** BigInt(scale - left.scale), right.units * 10n ** BigInt(scale - right.scale)]
}

function isMultipleOf(value: string | number, step: number, decimal: boolean | undefined): boolean {
  if (decimal) {
    const [units, divisor] = decimalOperands(value, step)
    return units % divisor === 0n
  }
  const ratio = Number(value) / step
  return Math.abs(ratio - Math.round(ratio)) <= 1e-9
}

function validateNumericParam(value: number | string, spec: AlgorithmParamSchema, label: string): string | null {
  const compare = (bound: number) => {
    if (!spec.decimal) return Number(value) - bound
    const [left, right] = decimalOperands(value, bound)
    return left < right ? -1 : left > right ? 1 : 0
  }
  if (spec.minimum !== undefined && compare(spec.minimum) < 0) return `${label}不能小于 ${spec.minimum}`
  if (spec.maximum !== undefined && compare(spec.maximum) > 0) return `${label}不能大于 ${spec.maximum}`
  if (spec.exclusiveMinimum !== undefined && compare(spec.exclusiveMinimum) <= 0) return `${label}必须大于 ${spec.exclusiveMinimum}`
  if (spec.exclusiveMaximum !== undefined && compare(spec.exclusiveMaximum) >= 0) return `${label}必须小于 ${spec.exclusiveMaximum}`
  if (spec.multipleOf !== undefined && !isMultipleOf(value, spec.multipleOf, spec.decimal)) return `${label}必须按 ${spec.multipleOf} 递增`
  return null
}

function validateParamValue(value: unknown, spec: AlgorithmParamSchema, label: string): string | null {
  switch (spec.type) {
    case 'boolean': return typeof value === 'boolean' ? null : `${label}必须是布尔值`
    case 'string': return typeof value === 'string' ? null : `${label}必须是字符串`
    case 'integer':
      return typeof value === 'number' && Number.isInteger(value) ? validateNumericParam(value, spec, label) : `${label}必须是整数`
    case 'number': {
      const decimalString = spec.decimal && typeof value === 'string' && new RegExp(spec.pattern!).test(value.trim())
      if (decimalString || (typeof value === 'number' && Number.isFinite(value))) return validateNumericParam(value as number | string, spec, label)
      return `${label}必须是数字`
    }
  }
}

/** 按 JSON Schema 的 primitive 约束校验算法参数。 */
export function validateAlgorithmSchemaParams(
  params: Record<string, unknown>,
  schema: Record<string, unknown>,
): string | null {
  const fields = algorithmSchemaFields(schema)
  if (!fields) return null
  for (const field of fields) {
    const value = params[field.name]
    if (value === undefined) {
      if (field.required && field.schema.default === undefined) return `${field.schema.title || field.name}为必填参数`
      continue
    }
    const label = field.schema.title || field.name
    if (value === null && field.schema.nullable) continue
    const error = validateParamValue(value, field.schema, label)
    if (error) return error
    if (field.schema.enum && !field.schema.enum.includes(value)) return `${label}不在可选范围内`
  }
  return null
}

/** 枚举文案来自后端；扩展不参与参数校验。 */
export function algorithmFieldOptions(spec: AlgorithmParamSchema) {
  const values = spec.enum ?? (spec['x-enum-labels'] ? Object.keys(spec['x-enum-labels']) : null)
  return values?.map((value) => ({ value: String(value), label: spec['x-enum-labels']?.[String(value)] ?? String(value) })) ?? null
}

/** 数值单位换算只影响显示，写回时除以同一比例。 */
export function algorithmNumericDisplay(spec: AlgorithmParamSchema, value: unknown) {
  const rawScale = spec['x-display-scale']
  const scale = typeof rawScale === 'number' && Number.isFinite(rawScale) && rawScale > 0 ? rawScale : 1
  const minimum = spec.exclusiveMinimum ?? spec.minimum
  const maximum = spec.exclusiveMaximum ?? spec.maximum
  return {
    scale,
    value: typeof value === 'number' ? value * scale : Number.NaN,
    min: minimum === undefined ? undefined : minimum * scale,
    max: maximum === undefined ? undefined : maximum * scale,
    exclusiveMin: spec.exclusiveMinimum !== undefined,
    step: spec['x-display-step'] ?? (spec.multipleOf === undefined ? (spec.type === 'integer' ? 1 : 0.1) : spec.multipleOf * scale),
    suffix: spec['x-unit'],
  }
}
