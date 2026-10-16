import { describe, expect, test } from 'bun:test'
import type { ConnectionTable } from '@/lib/api/connectionProgress'
import { selectConnectionRows, connectionCellText } from './connectionTable'

const table: ConnectionTable = {
  kind: 'table', title: '记录', summary: '4 条', row_count: 4,
  columns: [{ key: 'id', label: '编号', type: 'text' }, { key: 'quantity', label: '手数', type: 'number' }, { key: 'flag', label: '标志', type: 'text' }],
  rows: [['000123', 10, false], ['000124', 2, true], ['000125', null, true], ['000126', 0, false]],
}
describe('查询快照表格', () => {
  test('排序按数值比较，双向都将空值置末且不修改快照', () => {
    expect(selectConnectionRows(table, '', {}, { key: 'quantity', descending: false })).toEqual([3, 1, 0, 2])
    expect(selectConnectionRows(table, '', {}, { key: 'quantity', descending: true })).toEqual([0, 1, 3, 2])
    expect(table.rows[0]).toEqual(['000123', 10, false])
  })
  test('搜索保留前导零，能与筛选叠加', () => {
    expect(selectConnectionRows(table, '00012', { flag: 'false' }, null)).toEqual([0, 3])
    expect(selectConnectionRows(table, 'false', {}, null)).toEqual([0, 3])
    expect(selectConnectionRows(table, '000123', { flag: 'true' }, null)).toEqual([])
  })
  test('零、false 与空值分别展示', () => {
    expect(connectionCellText(0)).toBe('0')
    expect(connectionCellText(false)).toBe('false')
    expect(connectionCellText(null)).toBe('未返回')
    expect(connectionCellText('')).toBe('未返回')
  })
})
