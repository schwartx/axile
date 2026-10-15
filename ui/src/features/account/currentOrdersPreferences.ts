export type OrderFetchMode = 'manual' | 'automatic'

function storageKey(accountId: number): string {
  return `axile:orders:${location.origin}:${accountId}:fetch-mode`
}

export function readOrderFetchMode(accountId: number): OrderFetchMode {
  try {
    return localStorage.getItem(storageKey(accountId)) === 'automatic' ? 'automatic' : 'manual'
  } catch {
    return 'manual'
  }
}

export function writeOrderFetchMode(accountId: number, mode: OrderFetchMode): void {
  try { localStorage.setItem(storageKey(accountId), mode) } catch { /* 当前会话仍生效。 */ }
}
