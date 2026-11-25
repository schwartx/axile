import { initialSignalDraft } from '@/features/signal/signalData'
import { CodeBlock, DeveloperDoc, DocTable, RichText, RuleList, SectionTitle } from './DeveloperDoc'

const TITLE = '自定义行情函数'
const INTRO = '为调仓信号账户编写 `fetch_quotes(symbols)`，把你的行情源转换成 Axile 的统一行情快照。'
const SECTIONS = [['contract', '函数契约'], ['fields', '行情字段'], ['example', '可复制示例'], ['test', '测试行情']] as const
const CONTRACT = [
  '定义同步函数 `fetch_quotes(symbols: list[str]) -> dict[str, UnifiedPriceData]`。`symbols` 是本次要查询的标的列表。',
  '用请求中的标的代码作为字典键，大小写保持一致。每个值是 `axile.executor.models.unified_price.UnifiedPriceData` 的实例，`symbol` 填同一个标的代码。',
  '组合调用 `context.get_price(symbol)` 或 `context.get_quote(symbol)` 时按需取价，分别读取最新成交价和整份快照。正式计算缺少所请求的行情时会报错。',
]
const FIELDS = [
  ['`symbol`', '标的代码，与返回字典的键一致。'],
  ['`last_price`', '最新成交价。'],
  ['`bids` / `asks`', '每档用 `(价格, 数量)` 表示。买盘按价格从高到低，卖盘从低到高，买一和卖一放在各自列表首位。没有对应盘口数据时用空列表。'],
  ['`volume`', '成交量。'],
  ['`timestamp` / `update_time`', '行情源的更新时间，分别用 Unix 毫秒整数和 ISO 8601 字符串表示。'],
  ['`turnover` / `extra`', '可选。分别为成交额和行情源补充字段。'],
]
const EXAMPLE_INTRO = '示例为每个请求标的返回固定价格和盘口，用于验证函数接入。正式使用时，将示例的价格、盘口、成交量和时间替换为行情源实际返回的数据。'
const EXAMPLE = initialSignalDraft.quoteCode.trimEnd()
const TEST = [
  '把示例粘贴到行情函数编辑器。输入要查询的标的（最多 10 个），点击「测试行情」即可运行当前草稿。',
  '逐个核对最新价、时间和买卖盘。返回字典缺少某个标的时，结果会单独标出该标的。',
]

function buildMarkdown() {
  return [
    `# ${TITLE}`, INTRO,
    '## 函数契约', CONTRACT.map((rule) => `- ${rule}`).join('\n'),
    '## 行情字段', ['| 字段 | 含义 |', '| --- | --- |', ...FIELDS.map(([field, meaning]) => `| ${field} | ${meaning} |`)].join('\n'),
    '## 可复制示例', EXAMPLE_INTRO, `\`\`\`python\n${EXAMPLE}\n\`\`\``,
    '## 测试行情', TEST.map((rule) => `- ${rule}`).join('\n'),
  ].join('\n\n') + '\n'
}

export function SignalQuotesDocPage() {
  return (
    <DeveloperDoc category="账户 / 行情" title={TITLE} intro={<RichText text={INTRO} />} sections={SECTIONS} markdown={buildMarkdown}>
      <SectionTitle id="contract">函数契约</SectionTitle>
      <RuleList items={CONTRACT} />

      <SectionTitle id="fields">行情字段</SectionTitle>
      <DocTable headers={['字段', '含义']} rows={FIELDS.map(([field, meaning]) => [
        <RichText key="field" text={field} />, <RichText key="meaning" text={meaning} />,
      ])} />

      <SectionTitle id="example">可复制示例</SectionTitle>
      <p className="mb-4 mt-2 text-[15px] leading-7 text-ink-2">{EXAMPLE_INTRO}</p>
      <CodeBlock code={EXAMPLE} />

      <SectionTitle id="test">测试行情</SectionTitle>
      <RuleList items={TEST} />
    </DeveloperDoc>
  )
}
