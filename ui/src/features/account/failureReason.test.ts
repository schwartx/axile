import { expect, test } from 'bun:test'
import { describeFailureText } from './failureReason'

test.each([['0.062', '1.401'], ['-0.062', '-1.401'], ['0', '1e-07']])(
  '持仓未达目标提示精简文案并保留原文：%s / %s',
  (current, target) => {
    const raw = `目标未完成: 当前持仓=${current}，目标持仓=${target}`
    expect(describeFailureText(raw)).toEqual({ human: `持仓未达目标：${current} / ${target}`, raw })
  },
)

test.each(['-2019 Margin is insufficient', 'CTP 交易前置断线: 4097', '{"status":1000}', 'CLOSED', ''])(
  '错误原文直接回放，不分类或建议重试：%s',
  raw => {
    expect(describeFailureText(raw)).toEqual({ human: raw, raw })
  },
)
