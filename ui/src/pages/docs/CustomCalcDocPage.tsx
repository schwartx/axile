import { TriangleAlert } from 'lucide-react'
import { CodeBlock, DeveloperDoc, DocTable, InlineCode, RuleList, SectionTitle } from './DeveloperDoc'
import { buildCustomCalcMarkdown, CALC_EXAMPLES, CONTEXT_FIELDS, CONTEXT_MODES, CONTRACT_CODE, CONTRACT_RULES, EXECUTION_TRIGGERS, EXECUTOR_RULES } from './customCalcMarkdown'

const SECTIONS = [['execution', '何时执行'], ['contexts', '运行上下文'], ['contract', '函数契约'], ['api', 'Context API'], ['executor', '完整 executor'], ['examples', '示例']] as const

export function CustomCalcDocPage() {
  return (
    <DeveloperDoc
      category="组合"
      title="自定义组合函数"
      intro={<>用 <InlineCode>calculate_portfolio(context)</InlineCode> 计算组合的原始目标权重。Context 提供跨渠道的统一查询；高级代码也可以直接使用当前账户的完整 executor。</>}
      sections={SECTIONS}
      markdown={buildCustomCalcMarkdown}
    >
      <SectionTitle id="execution">何时执行</SectionTitle>
      <p className="mt-2 text-[15px] leading-7 text-ink-2">函数只在明确需要重新计算目标时运行：</p>
      <RuleList items={EXECUTION_TRIGGERS} />
      <p className="mt-4 border-l-2 border-accent pl-4 text-[14.5px] leading-7 text-ink-2">普通页面读取只显示最近一次成功保存的目标快照，不会执行用户函数。</p>

      <SectionTitle id="contexts">无账户试跑与真实账户</SectionTitle>
      <DocTable headers={['能力', '无账户试跑', '真实账户']} rows={CONTEXT_MODES.map((mode) => [mode.item, mode.sample, mode.real])} />
      <div className="mt-4 rounded-[8px] border border-warn/35 bg-warn/5 px-4 py-3.5 text-[14.5px] leading-7 text-ink-2">
        <div className="flex items-center gap-2 font-[600] text-warn"><TriangleAlert size={16} />真实账户执行边界</div>
        <p className="mt-1">“试跑”只表示 Axile 不会自动执行函数返回的目标；如果函数主动调用 executor 的交易方法，仍会产生真实交易。</p>
      </div>

      <SectionTitle id="contract">函数契约</SectionTitle>
      <div className="mt-4"><CodeBlock code={CONTRACT_CODE} /></div>
      <RuleList items={CONTRACT_RULES} />

      <SectionTitle id="api">Context 通用能力</SectionTitle>
      <p className="mt-2 text-[15px] leading-7 text-ink-2">优先使用这些跨渠道能力。账户和行情缓存只在当前一次函数调用中有效。</p>
      <DocTable headers={['字段或方法', '类型', '含义']} minWidth={760} rows={CONTEXT_FIELDS.map((field) => [<code key="name" className="font-mono text-[13px] text-ink-1">{field.name}</code>, <code key="type" className="font-mono text-[13px] text-ink-3">{field.type}</code>, field.desc])} />

      <SectionTitle id="executor">完整 executor</SectionTitle>
      <p className="mt-2 text-[15px] leading-7 text-ink-2">这是面向可信高级代码的入口，不是额外包装的只读接口。</p>
      <RuleList items={EXECUTOR_RULES} />

      <SectionTitle id="examples">示例</SectionTitle>
      <div className="mt-4 flex flex-col gap-8">
        {CALC_EXAMPLES.map((example) => <section key={example.key}>
          <div className="flex items-center gap-2"><h3 className="text-[16px] font-[600]">{example.title}</h3>{example.advanced && <span className="rounded bg-fill px-2 py-0.5 text-xs text-ink-3">高级</span>}</div>
          <p className="mb-3 mt-1 text-[14.5px] leading-6 text-ink-2">{example.desc}</p>
          <CodeBlock code={example.code} />
        </section>)}
      </div>
    </DeveloperDoc>
  )
}
