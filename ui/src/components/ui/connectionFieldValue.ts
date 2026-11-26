import type { ChannelAccountField, ChannelAccountFieldConstraints } from '@/types/api'

export type ConnectionFieldKind = Exclude<ChannelAccountField['kind'], 'boolean' | 'select'>

export interface ConnectionValidationContext {
  kind: ConnectionFieldKind
  value: string
  required: boolean
  label: string
  placeholder?: string
  constraints?: ChannelAccountFieldConstraints | null
}

const MONEY_PATTERN = /^[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)$/

function hasControlCharacters(value: string): boolean {
  return Array.from(value).some((character) => {
    const code = character.charCodeAt(0)
    return code <= 31 || code === 127
  })
}

/** 整理手工粘贴与显式剪贴板读取的值；不改写密钥中的有效空格。 */
export function normalizeConnectionValue(kind: ConnectionFieldKind, raw: string): string {
  let value = kind === 'secret' ? raw.replace(/(?:\r\n|\r|\n)+$/g, '') : raw.trim()
  if (kind === 'directory' && value.length >= 2) {
    const first = value[0]
    const last = value[value.length - 1]
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) value = value.slice(1, -1)
  }
  if (/\r|\n/.test(value)) throw new Error('一次只能粘贴一项内容')
  return value
}

/** 允许录入中的小数点和千分位，完整金额仍由 normalizeMoneyValue 校验。 */
export function isDecimalDraft(value: string, signed = false): boolean {
  return /^[\d,]*(?:\.\d*)?$/.test(signed ? value.replace(/^[+-]/, '') : value)
}

/** 返回无歧义金额的规范化表示；非法格式返回 null。 */
export function normalizeMoneyValue(raw: string): string | null {
  const value = raw.trim()
  if (!MONEY_PATTERN.test(value)) return null
  const normalized = value.replace(/,/g, '')
  return Number.isFinite(Number(normalized)) ? normalized : null
}

function validPort(port: string): boolean {
  if (!/^\d+$/.test(port)) return false
  const number = Number(port)
  return Number.isInteger(number) && number >= 1 && number <= 65535
}

function validHost(host: string): boolean {
  if (!host || /\s/.test(host) || hasControlCharacters(host)) return false
  const unwrapped = host.startsWith('[') && host.endsWith(']') ? host.slice(1, -1) : host
  if (unwrapped.includes(':')) {
    try {
      const parsed = new URL(`http://[${unwrapped}]:1`)
      return Boolean(parsed.hostname)
    } catch {
      return false
    }
  }
  if (/^\d+(?:\.\d+){3}$/.test(unwrapped)) {
    return unwrapped.split('.').every((part) => Number(part) >= 0 && Number(part) <= 255)
  }
  if (/^[\d.]+$/.test(unwrapped)) return false
  if (unwrapped.length > 253) return false
  return unwrapped.split('.').every((part) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(part))
}

function endpointExample(context: ConnectionValidationContext): string {
  const endpoint = context.constraints?.endpoint
  const port = endpoint?.port === 'required' ? ':端口' : '[:端口]'
  if (endpoint?.scheme === 'required' && endpoint.allowed_schemes.length === 1) {
    return `请填写 ${endpoint.allowed_schemes[0]}://主机${port}`
  }
  if (context.placeholder && !context.placeholder.includes('...')) {
    return `请填写${context.label}，例如 ${context.placeholder.replace(/^如\s*/, '')}`
  }
  if (endpoint?.scheme === 'forbidden') return `请填写主机${port}`
  return `请输入有效的${context.label}`
}

function authorityPort(authority: string): { host: string; port: string | null; malformed: boolean } {
  const withoutCredentials = authority.slice(authority.lastIndexOf('@') + 1)
  if (withoutCredentials.startsWith('[')) {
    const end = withoutCredentials.indexOf(']')
    if (end < 0) return { host: '', port: null, malformed: true }
    const remainder = withoutCredentials.slice(end + 1)
    if (remainder && !remainder.startsWith(':')) return { host: '', port: null, malformed: true }
    return {
      host: withoutCredentials.slice(0, end + 1),
      port: remainder.startsWith(':') ? remainder.slice(1) : null,
      malformed: false,
    }
  }
  const colon = withoutCredentials.lastIndexOf(':')
  if (colon < 0) return { host: withoutCredentials, port: null, malformed: false }
  if (withoutCredentials.slice(0, colon).includes(':')) return { host: '', port: null, malformed: true }
  return { host: withoutCredentials.slice(0, colon), port: withoutCredentials.slice(colon + 1), malformed: false }
}

/** 按字段声明校验地址结构；不做 DNS、端口探测或鉴权。 */
export function endpointValueError(context: ConnectionValidationContext): string | null {
  const value = context.value.trim()
  const constraint = context.constraints?.endpoint
  const schemePolicy = constraint?.scheme ?? 'optional'
  const allowedSchemes = constraint?.allowed_schemes ?? ['tcp', 'http', 'https', 'ftp', 'ws', 'wss']
  const portPolicy = constraint?.port ?? 'required'
  const allowPath = constraint?.allow_path ?? false
  const schemeMatch = value.match(/^([a-z][a-z0-9+.-]*):\/\//i)

  if (schemeMatch) {
    const scheme = schemeMatch[1].toLowerCase()
    const authority = value.slice(schemeMatch[0].length).split(/[/?#]/, 1)[0]
    const parts = authorityPort(authority)
    if (parts.malformed || !parts.host) return endpointExample(context)
    if (parts.port !== null && !validPort(parts.port)) return '端口必须是 1–65535 的整数'
    if (portPolicy === 'required' && parts.port === null) return endpointExample(context)
    if (schemePolicy === 'forbidden') return endpointExample(context)
    if (!allowedSchemes.some((allowed) => allowed === scheme)) return `不支持 ${scheme}:// 协议`
    if (!validHost(parts.host)) return '主机地址格式不正确'
    try {
      new URL(value)
      const suffix = value.slice(schemeMatch[0].length + authority.length)
      if (!allowPath && suffix) {
        return '该地址不能包含路径、查询参数或片段'
      }
    } catch {
      return endpointExample(context)
    }
    return null
  }

  if (schemePolicy === 'required') return endpointExample(context)
  const parts = authorityPort(value)
  if (parts.malformed || !parts.host || parts.port === null) return endpointExample(context)
  if (!validPort(parts.port)) return '端口必须是 1–65535 的整数'
  if (!validHost(parts.host)) return '主机地址格式不正确'
  return null
}

/** 判断字符串是否为 Windows 绝对目录。 */
export function isWindowsAbsoluteDirectory(raw: string): boolean {
  const value = raw.trim()
  if (hasControlCharacters(value)) return false
  if (/^[a-z]:[\\/]/i.test(value)) {
    const rest = value.slice(3)
    return !/[<>:"|?*]/.test(rest)
  }
  const unc = value.match(/^\\\\([^\\/]+)[\\/]([^\\/]+)(.*)$/)
  if (!unc) return false
  return !/[<>:"|?*]/.test(`${unc[1]}${unc[2]}${unc[3]}`)
}

/** 返回连接字段的首个确定性结构错误。 */
export function connectionValueError(context: ConnectionValidationContext): string | null {
  const { kind, value, required, label } = context
  let comparable = kind === 'secret' ? value : value.trim()
  if (kind === 'directory' && comparable.length >= 2) {
    const first = comparable[0]
    const last = comparable[comparable.length - 1]
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) comparable = comparable.slice(1, -1)
  }
  if (required && comparable.trim() === '') return `请填写${label}`
  if (comparable === '') return null
  if (hasControlCharacters(comparable)) return '一次只能填写一项内容'
  if (kind === 'endpoint') return endpointValueError(context)
  if (kind === 'directory') return isWindowsAbsoluteDirectory(comparable) ? null : '请输入 Windows 绝对路径'
  if (kind === 'money') {
    const normalized = normalizeMoneyValue(comparable)
    if (normalized === null) return '请输入有效金额'
    const number = Number(normalized)
    const constraints = context.constraints?.number
    if (constraints?.gt != null && number <= constraints.gt) return `金额必须大于 ${constraints.gt}`
    if (constraints?.gte != null && number < constraints.gte) return `金额不能小于 ${constraints.gte}`
  }
  return null
}
