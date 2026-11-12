/** 精简已知持仓提示，其余错误原样回放；原始详情始终保留。 */
export interface FailureReason {
  human: string
  raw: string
}
export function describeFailureText(text: string): FailureReason {
  return {
    human: text.replace(/^目标未完成: 当前持仓=([^，\r\n]+)，目标持仓=([^，\r\n]+)$/, '持仓未达目标：$1 / $2'),
    raw: text,
  }
}
