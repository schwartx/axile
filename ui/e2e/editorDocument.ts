import { EditorView } from '@codemirror/view'

/** 只读断言读取真实文档，避免把异步类型提示当成源码变化。 */
export function visibleEditorCode(): string | null {
  const content = [...document.querySelectorAll<HTMLElement>('.cm-content[aria-readonly="true"]')]
    .find(node => node.getClientRects().length > 0)
  return content ? EditorView.findFromDOM(content)?.state.doc.toString() ?? null : null
}
