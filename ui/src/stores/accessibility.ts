import { create } from 'zustand'

export type MarketPalette = 'red-up' | 'green-up' | 'blue-up' | 'purple-up' | 'red-cyan' | 'monochrome'

export interface AccessibilityPreferences {
  directionMarkers: boolean
  enhancedContrast: boolean
  marketPalette: MarketPalette
}

const STORAGE_KEY = 'axile-accessibility'
const DEFAULTS: AccessibilityPreferences = { enhancedContrast: false, marketPalette: 'red-up', directionMarkers: false }

/** 与 index.html 首屏恢复保持相同的逐字段校验。 */
function parsePreferences(raw: string | null): AccessibilityPreferences {
  try {
    const value: unknown = JSON.parse(raw ?? 'null')
    if (!value || typeof value !== 'object') return { ...DEFAULTS }
    const stored = value as Record<string, unknown>
    return {
      enhancedContrast: stored.enhancedContrast === true,
      directionMarkers: stored.directionMarkers === true,
      marketPalette: stored.marketPalette === 'green-up' || stored.marketPalette === 'blue-up' || stored.marketPalette === 'purple-up' || stored.marketPalette === 'red-cyan' || stored.marketPalette === 'monochrome' ? stored.marketPalette : 'red-up',
    }
  } catch {
    return { ...DEFAULTS }
  }
}

function apply(preferences: AccessibilityPreferences) {
  const root = document.documentElement.classList
  root.toggle('contrast-more', preferences.enhancedContrast)
  root.toggle('market-green-up', preferences.marketPalette === 'green-up')
  root.toggle('market-purple-up', preferences.marketPalette === 'purple-up')
  root.toggle('market-blue-up', preferences.marketPalette === 'blue-up')
  root.toggle('market-red-cyan', preferences.marketPalette === 'red-cyan')
  root.toggle('market-monochrome', preferences.marketPalette === 'monochrome')
  root.toggle('direction-markers', preferences.directionMarkers || preferences.marketPalette === 'monochrome')
}

function readInitial() {
  try {
    const preferences = parsePreferences(localStorage.getItem(STORAGE_KEY))
    // 提前发现只读存储；探针不改动用户的偏好键，也不触发偏好同步。
    const probe = `${STORAGE_KEY}-probe`
    localStorage.setItem(probe, '1')
    localStorage.removeItem(probe)
    return { ...preferences, storageAvailable: true }
  } catch {
    return { ...DEFAULTS, storageAvailable: false }
  }
}

interface AccessibilityState extends AccessibilityPreferences {
  storageAvailable: boolean
  update: (patch: Partial<AccessibilityPreferences>) => void
}

export const useAccessibilityStore = create<AccessibilityState>((set, get) => {
  const initial = readInitial()
  apply(initial)
  return {
    ...initial,
    update: (patch) => {
      const preferences: AccessibilityPreferences = {
        enhancedContrast: patch.enhancedContrast ?? get().enhancedContrast,
        directionMarkers: patch.directionMarkers ?? get().directionMarkers,
        marketPalette: patch.marketPalette ?? get().marketPalette,
      }
      let storageAvailable = true
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences)) }
      catch { storageAvailable = false }
      apply(preferences)
      set({ ...preferences, storageAvailable })
    },
  }
})

window.addEventListener('storage', (event) => {
  if (event.key !== STORAGE_KEY && event.key !== null) return
  try { if (event.storageArea !== window.localStorage) return }
  catch { return }
  const preferences = parsePreferences(event.newValue)
  apply(preferences)
  useAccessibilityStore.setState(preferences)
})
