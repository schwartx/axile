import { expect, test } from 'bun:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { spawnSync } from 'node:child_process'
import { SchemaEditor } from './AlgorithmEditor'
import type { AlgorithmInfo } from '@/types/api'

function metadata(description: string): AlgorithmInfo {
  return { name: 'SINGLE-MAKER', label: '后端名称', description: '算法原理', builtin: true, channels: null, slots: null,
    default_params: { chase_enabled: false, chase_ticks: 1 },
    params_schema: { type: 'object', properties: {
      chase_enabled: { type: 'boolean', title: '后端追单标题', description: '开关说明', default: false },
      chase_ticks: { type: 'integer', title: '后端阈值标题', description, default: 1, minimum: 0 },
    } } }
}

test('关闭追单仍完整显示可编辑字段，文案随服务端改变', () => {
  const render = (description: string) => renderToStaticMarkup(createElement(SchemaEditor, { info: metadata(description), params: { chase_enabled: false, chase_ticks: 7 }, onChange: () => {} }))
  const html = render('服务端说明甲')
  expect(html).toContain('后端追单标题')
  expect(html).toContain('后端阈值标题')
  expect(html).toContain('服务端说明甲')
  expect(html).toContain('value="7"')
  expect(html).not.toContain('disabled')
  expect(html).not.toContain('高级设置')
  expect(html).not.toContain('省成本')
  expect(render('服务端说明乙')).toContain('服务端说明乙')
})

test('服务端提示决定控件，百分比历史精度不按滑块步长取整', () => {
  const info = metadata('说明')
  info.params_schema = { type: 'object', properties: {
    mode: { type: 'string', title: '模式', enum: ['a', 'b'], 'x-control': 'cards', 'x-option-descriptions': { a: '选项独立说明' } },
    rate: { type: 'number', title: '参与率', exclusiveMinimum: 0, maximum: 1,
      'x-control': 'slider', 'x-unit': '%', 'x-display-scale': 100, 'x-display-step': 0.01, 'x-slider-min': 0.01, 'x-slider-max': 100 },
    duration: { type: 'integer', title: '时长', 'x-control': 'presets', 'x-presets': [60, 300] },
  } }
  const html = renderToStaticMarkup(createElement(SchemaEditor, { info, params: { mode: 'a', rate: 0.000012345, duration: 300 }, onChange: () => {} }))
  expect(html).toContain('role="radiogroup"')
  expect(html).toContain('选项独立说明')
  expect(html).toContain('role="slider"')
  expect(html).not.toContain('type="range"')
  expect(html).toContain('value="0.0012345"')
  expect(html).toContain('aria-pressed="true"')
  expect(html).not.toContain('role="alert"')
})

test('参与率独立 NumberFlow 保留小于一个步长的小数，无滑条', () => {
  const info = metadata('说明')
  info.params_schema = { type: 'object', properties: {
    rate: { type: 'number', title: '参与率', exclusiveMinimum: 0, maximum: 1,
      'x-control': 'numberflow', 'x-unit': '%', 'x-display-scale': 100, 'x-display-step': 1 },
  } }
  const html = renderToStaticMarkup(createElement(SchemaEditor, { info, params: { rate: 0.005 }, onChange: () => {} }))
  expect(html).toContain('number-flow-react')
  expect(html).toContain('value="0.5"')
  expect(html).toContain('增加 1 %')
  expect(html).not.toContain('role="slider"')
  expect(html).not.toContain('role="alert"')
})

test('整数秒参数共用时长输入与时分秒预设，小数秒仍保留原有精度', () => {
  const info = metadata('说明')
  info.params_schema = { type: 'object', properties: {
    duration: { type: 'integer', title: '执行时长', minimum: 1, maximum: 86400, 'x-unit': '秒',
      'x-control': 'presets', 'x-presets': [60, 300, 1800] },
    wait: { type: 'integer', title: '最大等待', minimum: 1, maximum: 3600, 'x-unit': '秒' },
    interval: { type: 'number', title: '间隔', minimum: 0.1, 'x-unit': '秒', 'x-control': 'stepper' },
    optional: { title: '可空时长', anyOf: [{ type: 'integer', minimum: 1 }, { type: 'null' }],
      default: null, 'x-unit': '秒', 'x-control': 'stepper' },
  } }
  const html = renderToStaticMarkup(createElement(SchemaEditor, {
    info, params: { duration: 4830, wait: 210, interval: 0.12345 }, onChange() {},
  }))
  expect(html).toContain('aria-label="执行时长：时"')
  expect(html).toContain('aria-label="最大等待：分"')
  expect(html).toContain('>30 分</button>')
  expect(html).toContain('value="0.12345"')
  expect(html).toContain('inputMode="decimal"')
  expect(html).not.toContain('aria-label="可空时长：分"')
  expect(html).not.toContain('role="alert"')
})

test('Decimal 与可空价格显示结构化表单，原有数值和字符串精度不丢失', () => {
  const info = metadata('说明')
  info.name = 'CUSTOM-DECIMAL'
  info.params_schema = { type: 'object', properties: {
    amount: { title: '单轮开仓金额上限', anyOf: [{ type: 'number', exclusiveMinimum: 0 },
      { type: 'string', pattern: '^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$' }] },
    price: { title: '已核对的保本价', default: null, anyOf: [{ type: 'number', exclusiveMinimum: 0 },
      { type: 'string', pattern: '^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$' }, { type: 'null' }] },
    rounds: { title: '可空轮次', anyOf: [{ type: 'integer', minimum: 1 }, { type: 'null' }], default: null },
    enabled: { title: '可空开关', anyOf: [{ type: 'boolean' }, { type: 'null' }], default: null },
  } }
  const render = (amount: number | string) => renderToStaticMarkup(createElement(SchemaEditor, {
    info, params: { amount }, onChange: () => {},
  }))
  const html = render('6.123456789012345678901')
  expect(html).toContain('单轮开仓金额上限')
  expect(html).toContain('已核对的保本价')
  expect(html).toContain('value="6.123456789012345678901"')
  expect(html).toContain('inputMode="decimal"')
  expect(html).toContain('placeholder="未设置"')
  expect(html).not.toContain('textarea')
  expect(html).not.toContain('role="alert"')
  expect(render(6)).toContain('value="6"')
})

test('编辑 Decimal 不转换为浮点数，清空可空价格写回 null，必填字段留空仍报错', () => {
  const result = spawnSync(process.execPath, ['--eval', `
    import { expect, mock } from 'bun:test';
    const React = await import('react');
    mock.module('react', () => ({ ...React, useId: () => 'hint' }));
    const { SchemaFieldRow } = await import('./src/features/setup/AlgorithmEditor.tsx');
    const { algorithmSchemaFields, validateAlgorithmSchemaParams } = await import('./src/features/setup/algorithmSchema.ts');
    const schema = { type: 'object', properties: { price: {
      anyOf: [{ type: 'number', exclusiveMinimum: 0 },
        { type: 'string', pattern: '^(?!^[-+.]*$)[+-]?0*\\\\d*\\\\.?\\\\d*$' }, { type: 'null' }],
    } } };
    const field = algorithmSchemaFields(schema)[0];
    let emitted;
    const input = SchemaFieldRow({ field, value: null, onChange: (next) => { emitted = next; } }).props.children[1];
    input.props.onChange({ target: { value: '6.123456789012345678901' } });
    expect(emitted).toBe('6.123456789012345678901');
    expect(JSON.parse(JSON.stringify({ price: emitted })).price).toBe(emitted);
    input.props.onChange({ target: { value: '' } });
    expect(emitted).toBeNull();
    field.schema.nullable = false;
    const required = SchemaFieldRow({ field, value: 6, onChange: (next) => { emitted = next; } }).props.children[1];
    required.props.onChange({ target: { value: '' } });
    expect(emitted).toBe('');
    expect(validateAlgorithmSchemaParams({ price: emitted }, schema)).toContain('必须是数字');
  `], { cwd: new URL('../../../', import.meta.url), encoding: 'utf8', timeout: 10_000 })
  expect(result.status, result.stderr).toBe(0)
})
