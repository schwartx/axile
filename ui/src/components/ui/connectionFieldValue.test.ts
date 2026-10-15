import { describe, expect, it } from 'bun:test'

import {
  connectionValueError,
  isWindowsAbsoluteDirectory,
  normalizeConnectionValue,
  normalizeMoneyValue,
  type ConnectionValidationContext,
} from './connectionFieldValue'
import type { ChannelAccountFieldConstraints } from '@/types/api'

function context(overrides: Partial<ConnectionValidationContext> = {}): ConnectionValidationContext {
  return {
    kind: 'text', value: 'value', required: true, label: '字段', ...overrides,
  }
}

const ctpEndpoint: ChannelAccountFieldConstraints = {
  endpoint: { scheme: 'required', allowed_schemes: ['tcp'], port: 'required', allow_path: false },
}
const gmEndpoint: ChannelAccountFieldConstraints = {
  endpoint: { scheme: 'forbidden', allowed_schemes: [], port: 'required', allow_path: false },
}
const proxyEndpoint: ChannelAccountFieldConstraints = {
  endpoint: {
    scheme: 'optional',
    allowed_schemes: ['http', 'https', 'ftp', 'ws', 'wss'],
    port: 'optional',
    allow_path: false,
  },
}

describe('normalizeConnectionValue', () => {
  it('整理普通字段与带引号的 Windows 目录', () => {
    expect(normalizeConnectionValue('identifier', '  account-1  ')).toBe('account-1')
    expect(normalizeConnectionValue('directory', ' "C:\\Program Files\\GoldMiner3" ')).toBe('C:\\Program Files\\GoldMiner3')
  })

  it('密钥只移除尾部换行并保留有效空格', () => {
    expect(normalizeConnectionValue('secret', ' key with spaces \r\n')).toBe(' key with spaces ')
  })

  it('拒绝粘贴多项内容', () => {
    expect(() => normalizeConnectionValue('secret', 'first\nsecond')).toThrow('一次只能粘贴一项内容')
  })
})

describe('connectionValueError', () => {
  it('使用字段名称提示必填并拒绝控制字符', () => {
    expect(connectionValueError(context({ value: ' ', label: '投资者号' }))).toBe('请填写投资者号')
    expect(connectionValueError(context({ value: 'first\nsecond', kind: 'identifier' }))).toBe('一次只能填写一项内容')
    expect(connectionValueError(context({ value: '', required: false }))).toBeNull()
  })

  it('按 CTP 约束要求 tcp 协议、主机与端口', () => {
    const base = { kind: 'endpoint' as const, label: '交易前置', constraints: ctpEndpoint }
    expect(connectionValueError(context({ ...base, value: 'tcp://host:10130' }))).toBeNull()
    expect(connectionValueError(context({ ...base, value: 'host:10130' }))).toBe('请填写 tcp://主机:端口')
    expect(connectionValueError(context({ ...base, value: 'http://host:10130' }))).toBe('不支持 http:// 协议')
    expect(connectionValueError(context({ ...base, value: 'tcp://host:70000' }))).toBe('端口必须是 1–65535 的整数')
    expect(connectionValueError(context({ ...base, value: 'tcp://host:10130/path' }))).toBe('该地址不能包含路径、查询参数或片段')
  })

  it('按 GM 约束只接受裸主机端口', () => {
    const base = { kind: 'endpoint' as const, label: '终端 RPC 地址', placeholder: '127.0.0.1:7001', constraints: gmEndpoint }
    expect(connectionValueError(context({ ...base, value: '127.0.0.1:7001' }))).toBeNull()
    expect(connectionValueError(context({ ...base, value: '[2001:db8::1]:7001' }))).toBeNull()
    expect(connectionValueError(context({ ...base, value: '123213' }))).toBe('请填写终端 RPC 地址，例如 127.0.0.1:7001')
    expect(connectionValueError(context({ ...base, value: 'tcp://host:7001' }))).toBe('请填写终端 RPC 地址，例如 127.0.0.1:7001')
    expect(connectionValueError(context({ ...base, value: '999.1.1.1:7001' }))).toBe('主机地址格式不正确')
  })

  it('代理字段允许受支持协议的默认端口或裸主机端口', () => {
    const base = { kind: 'endpoint' as const, label: '代理', placeholder: 'http://127.0.0.1:7890', constraints: proxyEndpoint }
    expect(connectionValueError(context({ ...base, value: 'https://proxy.example.com' }))).toBeNull()
    expect(connectionValueError(context({ ...base, value: 'proxy.example.com:7890' }))).toBeNull()
    expect(connectionValueError(context({ ...base, value: 'tcp://proxy.example.com:7890' }))).toBe('不支持 tcp:// 协议')
    expect(connectionValueError(context({ ...base, value: 'proxy.example.com' }))).toBe('请填写代理，例如 http://127.0.0.1:7890')
  })

  it('校验 Windows 目录并拒绝 POSIX、相对路径与非法字符', () => {
    expect(isWindowsAbsoluteDirectory('C:\\Program Files\\GoldMiner3')).toBe(true)
    expect(isWindowsAbsoluteDirectory('C:/GoldMiner3')).toBe(true)
    expect(isWindowsAbsoluteDirectory('\\\\server\\share\\GoldMiner3')).toBe(true)
    expect(isWindowsAbsoluteDirectory('/opt/goldminer3')).toBe(false)
    expect(isWindowsAbsoluteDirectory('.\\GoldMiner3')).toBe(false)
    expect(isWindowsAbsoluteDirectory('C:\\bad|name')).toBe(false)
    expect(connectionValueError(context({ kind: 'directory', value: '"C:\\GoldMiner3"' }))).toBeNull()
  })

  it('规范化合法金额并执行数值边界', () => {
    expect(normalizeMoneyValue('10,000,000')).toBe('10000000')
    expect(normalizeMoneyValue('1,00')).toBeNull()
    expect(normalizeMoneyValue('NaN')).toBeNull()
    expect(normalizeMoneyValue('1万')).toBeNull()
    expect(connectionValueError(context({ kind: 'money', value: '0', constraints: { number: { gt: 0 } } }))).toBe('金额必须大于 0')
    expect(connectionValueError(context({ kind: 'money', value: '0', constraints: { number: { gte: 0 } } }))).toBeNull()
  })
})
