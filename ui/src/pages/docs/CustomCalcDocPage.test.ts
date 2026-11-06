import { describe, expect, test } from 'bun:test'
import { buildCustomCalcMarkdown } from './customCalcMarkdown'

describe('customCalcMarkdown', () => {
  test('serializes every documentation section', () => {
    const markdown = buildCustomCalcMarkdown()

    expect(markdown).toStartWith('# 自定义组合函数')
    expect(markdown).toContain('## 何时执行')
    expect(markdown).toContain('普通页面读取只显示最近一次成功保存的目标快照')
    expect(markdown).toContain('## 无账户试跑与真实账户')
    expect(markdown).toContain('| 能力 | 无账户试跑 | 真实账户 |')
    expect(markdown).toContain('只有实际读取 Context 的账户、持仓、行情、订单或 executor 时，才需要选择账户')
    expect(markdown).not.toContain('固定样例资产')
    expect(markdown).toContain('## 函数契约')
    expect(markdown).toContain('```python\ndef calculate_portfolio(context):')
    expect(markdown).toContain('## Context 通用能力')
    expect(markdown).toContain('| `get_quote(symbol)` | `UnifiedPriceData` |')
    expect(markdown).toContain('## 完整 executor（高级）')
    expect(markdown).toContain('### 读取 TQ 合约信息')
  })

  test('states the live executor and worker recovery boundary precisely', () => {
    const markdown = buildCustomCalcMarkdown()

    expect(markdown).toContain('不会自动执行函数返回的目标')
    expect(markdown).toContain('主动调用 executor 的交易方法，仍会产生真实交易')
    expect(markdown).toContain('完整、共享且常驻的真实渠道执行器')
    expect(markdown).toContain('系统会丢弃 worker，并在下次调用时重建')
    expect(markdown).toContain('一次性子进程')
  })

  test('uses the unified quote for TQ contract metadata', () => {
    const markdown = buildCustomCalcMarkdown()

    expect(markdown).toContain('context.get_quote("KQ.m@SHFE.rb")')
    expect(markdown).toContain('quote.extra["volume_multiple"]')
    expect(markdown).toContain('quote.extra["price_tick"]')
  })
})
