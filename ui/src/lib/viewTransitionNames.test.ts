/**
 * 共享元素 FLIP 门禁：只允许约定的 viewTransitionName 前缀；
 * 禁止 history-chart / equity-viz 等内容 morph 共享名。
 */
import { describe, expect, it } from 'bun:test'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { editShellVtName } from '@/features/account/editShellVtName'
import { portfolioNameVtName } from '@/features/portfolio/viewTransition'

const UI_SRC = join(import.meta.dir, '..')

/** 允许的共享名角色前缀。 */
const ALLOWED_VT_PREFIXES = [
  'account-name-',
  'account-channel-',
  'portfolio-name-',
  'equity-amount-',
  'edit-shell-',
  'account-config-',
  'app-nav-selection',
  'algorithm-directory-row',
] as const

/** 禁止的内容 morph / 已撤共享名。 */
const FORBIDDEN_VT_SNIPPETS = ['history-chart-', 'equity-viz-'] as const

/** 递归收集 ui/src 下 .ts/.tsx/.css 源文件（跳过 *.test.*）。 */
function listSourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) {
      out.push(...listSourceFiles(p))
      continue
    }
    if (/\.test\.(ts|tsx)$/.test(name)) continue
    if (/\.(ts|tsx|css)$/.test(name)) out.push(p)
  }
  return out
}

/** 从源码抽出 viewTransitionName 赋值里的模板/字符串字面量。 */
function extractVtNameLiterals(src: string): string[] {
  const found: string[] = []
  const re =
    /viewTransitionName\s*:\s*(?:`([^`]+)`|'([^']+)'|"([^"]+)")/g
  let m: RegExpExecArray | null
  while ((m = re.exec(src)) != null) {
    found.push(m[1] ?? m[2] ?? m[3] ?? '')
  }
  // 也匹配 viewTransitionName: shellVtName 等间接赋值——只校验字面量出现处
  return found
}

describe('editShellVtName', () => {
  it('生成 edit-shell 前缀的同构壳共享名', () => {
    expect(editShellVtName(7, 'timer')).toBe('edit-shell-timer-7')
    expect(editShellVtName(12, 'algorithm')).toBe('edit-shell-algorithm-12')
    expect(editShellVtName(9, 'control')).toBe('edit-shell-control-9')
  })
})

describe('portfolioNameVtName', () => {
  it('为列表卡和详情标题生成同一个纯文字共享身份', () => {
    expect(portfolioNameVtName(7)).toBe('portfolio-name-7')
  })
})

describe('viewTransitionName inventory (FLIP-only)', () => {
  const files = listSourceFiles(UI_SRC)

  it('无 history-chart / equity-viz 共享名赋值或字面量', () => {
    const hits: string[] = []
    for (const file of files) {
      const text = readFileSync(file, 'utf8')
      for (const bad of FORBIDDEN_VT_SNIPPETS) {
        if (!text.includes(bad)) continue
        // 允许在注释里写「禁止 / 不挂」时提及，但不得出现实际赋值字面量
        if (new RegExp(`viewTransitionName[^\\n]*${bad}`).test(text)) {
          hits.push(`${relative(UI_SRC, file)}: viewTransitionName uses ${bad}`)
        }
        if (new RegExp(`[\\\`'"']${bad}`).test(text)) {
          hits.push(`${relative(UI_SRC, file)}: string literal ${bad}`)
        }
      }
    }
    expect(hits).toEqual([])
  })

  it('所有 viewTransitionName 字面量仅属允许集的 FLIP 前缀', () => {
    const bad: string[] = []
    for (const file of files) {
      const text = readFileSync(file, 'utf8')
      for (const lit of extractVtNameLiterals(text)) {
        // 模板可能含 ${id}；取前缀到第一个 $ 或结束
        const head = lit.split('${')[0] ?? lit
        const ok = ALLOWED_VT_PREFIXES.some((p) => head.startsWith(p) || lit.startsWith(p))
        if (!ok) {
          bad.push(`${relative(UI_SRC, file)}: ${lit}`)
        }
      }
    }
    expect(bad).toEqual([])
  })

  it('editShellVtName 返回值属于 edit-shell 允许集', () => {
    const name = editShellVtName(1, 'timer')
    expect(ALLOWED_VT_PREFIXES.some((p) => name.startsWith(p))).toBe(true)
    expect(FORBIDDEN_VT_SNIPPETS.some((p) => name.includes(p))).toBe(false)
  })
})
