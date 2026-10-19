import { create } from 'zustand'

export type ThemeMode = 'light' | 'dark' | 'system'

const STORAGE_KEY = 'axile-theme'

function systemPrefersDark(): boolean {
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

/** 计算某模式下的实际深浅。 */
function isDark(mode: ThemeMode): boolean {
  return mode === 'dark' || (mode === 'system' && systemPrefersDark())
}

/** 把实际深浅应用到 <html>。 */
function apply(mode: ThemeMode) {
  document.documentElement.classList.toggle('dark', isDark(mode))
}

function readStored(): ThemeMode {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return v === 'light' || v === 'dark' || v === 'system' ? v : 'system'
  } catch { return 'system' }
}

interface ThemeState {
  mode: ThemeMode
  setMode: (mode: ThemeMode) => void
}

/** 主题：浅 / 深 / 跟随系统。持久化到 localStorage，system 模式监听系统变化。 */
export const useThemeStore = create<ThemeState>((set) => {
  const initial = readStored()
  apply(initial)

  // system 模式下跟随系统实时变化。
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (useThemeStore.getState().mode === 'system') apply('system')
  })

  return {
    mode: initial,
    setMode: (mode) => {
      try { localStorage.setItem(STORAGE_KEY, mode) } catch { /* 存储不可用时仍切换当前会话。 */ }
      apply(mode)
      set({ mode })
    },
  }
})
