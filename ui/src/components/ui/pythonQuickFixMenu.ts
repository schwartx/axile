import type { EditorView } from '@codemirror/view'

export interface QuickFixMenuItem {
  title: string
  apply: () => void
}

type Box = Pick<DOMRect, 'left' | 'top' | 'bottom'>

/** 菜单优先贴在触发点下方，空间不足时移到上方并限制在视口内。 */
export function quickFixMenuPosition(anchor: Box, width: number, height: number, viewportWidth: number, viewportHeight: number) {
  const margin = 8
  return {
    left: Math.min(Math.max(anchor.left, margin), Math.max(margin, viewportWidth - width - margin)),
    top: anchor.bottom + height + 4 <= viewportHeight - margin
      ? anchor.bottom + 4
      : Math.max(margin, anchor.top - height - 4),
  }
}

let closeActiveMenu: (() => void) | null = null

/** 在 hover 按钮或光标附近显示紧凑的修复列表。 */
export function showQuickFixMenu(view: EditorView, anchor: Box, items: QuickFixMenuItem[], host?: HTMLElement, onDismiss?: () => void, unavailable = false): void {
  closeActiveMenu?.()
  const menu = document.createElement('div')
  menu.className = 'cm-python-quickFixMenu'
  menu.setAttribute('role', 'menu')
  menu.setAttribute('aria-label', '快速修复')
  menu.tabIndex = -1
  const buttons: HTMLButtonElement[] = []
  const close = (focusEditor = false, applied = false) => {
    document.removeEventListener('pointerdown', onOutside, true)
    document.removeEventListener('keydown', onKeyDown, true)
    window.removeEventListener('resize', onLayoutChange)
    window.removeEventListener('scroll', onLayoutChange, true)
    menu.remove()
    if (closeActiveMenu === closeMenu) closeActiveMenu = null
    if (focusEditor) view.focus()
    if (!applied) onDismiss?.()
  }
  const closeMenu = () => close()
  const onOutside = (event: PointerEvent) => { if (!menu.contains(event.target as Node)) close() }
  const onLayoutChange = (event: Event) => {
    if (event.target instanceof Node && menu.contains(event.target)) return
    close()
  }
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'Escape') { event.preventDefault(); close(true); return }
    if (!buttons.length || event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    event.preventDefault()
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
    buttons[(index + buttons.length + (event.key === 'ArrowDown' ? 1 : -1)) % buttons.length]?.focus()
  }
  if (items.length) {
    const heading = menu.appendChild(document.createElement('div'))
    heading.className = 'cm-python-quickFixMenu-heading'
    heading.textContent = '快速修复'
    const list = menu.appendChild(document.createElement('div'))
    list.className = 'cm-python-quickFixMenu-list'
    for (const item of items) {
      const button = list.appendChild(document.createElement('button'))
      button.type = 'button'
      button.setAttribute('role', 'menuitem')
      button.className = 'cm-python-quickFixMenu-item'
      const icon = button.appendChild(document.createElement('span'))
      icon.className = 'cm-python-quickFixMenu-icon'
      icon.setAttribute('aria-hidden', 'true')
      icon.innerHTML = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6m-5 4h4M8.1 14.2a6 6 0 1 1 7.8 0c-.7.6-.9 1.4-.9 2.3H9c0-.9-.2-1.7-.9-2.3Z"/></svg>'
      const label = button.appendChild(document.createElement('span'))
      label.textContent = item.title
      button.addEventListener('click', () => { close(false, true); item.apply() })
      buttons.push(button)
    }
  } else {
    const empty = menu.appendChild(document.createElement('div'))
    empty.className = 'cm-python-quickFixMenu-empty'
    if (unavailable) empty.style.color = 'var(--color-warn)'
    empty.textContent = unavailable ? '快速修复暂不可用，请查看代码检查中的错误详情' : '当前位置没有可用的快速修复'
  }
  if (unavailable && items.length) {
    const note = menu.appendChild(document.createElement('div'))
    note.className = 'cm-python-quickFixMenu-empty'
    note.style.color = 'var(--color-warn)'
    note.textContent = '部分修复读取失败，错误详情见代码检查。'
  }
  const container = host ?? document.body
  container.appendChild(menu)
  menu.addEventListener('focusout', () => {
    queueMicrotask(() => { if (menu.isConnected && !menu.contains(document.activeElement)) close() })
  })
  const bounds = menu.getBoundingClientRect()
  const position = quickFixMenuPosition(anchor, bounds.width, bounds.height, window.innerWidth, window.innerHeight)
  menu.classList.toggle('cm-python-quickFixMenu-above', position.top < anchor.top)
  menu.style.left = `${position.left}px`
  menu.style.top = `${position.top}px`
  closeActiveMenu = closeMenu
  document.addEventListener('pointerdown', onOutside, true)
  document.addEventListener('keydown', onKeyDown, true)
  window.addEventListener('resize', onLayoutChange)
  window.addEventListener('scroll', onLayoutChange, true)
  if (buttons.length) buttons[0].focus({ preventScroll: true })
  else menu.focus({ preventScroll: true })
}
