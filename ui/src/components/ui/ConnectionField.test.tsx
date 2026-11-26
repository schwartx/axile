import { expect, test } from 'bun:test'
import { renderToStaticMarkup } from 'react-dom/server'
import { ConnectionField } from './ConnectionField'

test('连接字段只给完整地址或显式示例提供填入操作', () => {
  const onChange = () => {}
  expect(renderToStaticMarkup(
    <ConnectionField label="RPC 地址" kind="endpoint" value="" placeholder="127.0.0.1:7001" onChange={onChange} />,
  )).toContain('aria-label="填入 127.0.0.1:7001"')
  expect(renderToStaticMarkup(
    <ConnectionField label="期货公司代码" kind="identifier" value="" placeholder="9999" fillablePlaceholder onChange={onChange} />,
  )).toContain('aria-label="填入 9999"')
  expect(renderToStaticMarkup(
    <ConnectionField label="交易前置" kind="endpoint" value="" placeholder="tcp://..." onChange={onChange} />,
  )).toContain('placeholder="tcp://..."')
  expect(renderToStaticMarkup(
    <ConnectionField label="账户名称" kind="text" value="" placeholder="给这个账户起个名字" onChange={onChange} />,
  )).toContain('placeholder="给这个账户起个名字"')
})

test('已有输入时隐藏可填提示', () => {
  expect(renderToStaticMarkup(
    <ConnectionField label="RPC 地址" kind="endpoint" value="192.0.2.10:7001" placeholder="127.0.0.1:7001" onChange={() => {}} />,
  )).not.toContain('aria-label="填入 127.0.0.1:7001"')
})
