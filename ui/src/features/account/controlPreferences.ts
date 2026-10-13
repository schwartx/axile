export const DEFAULT_CONTROL_METRICS: [string, string] = ['account:place_order:per_day', 'account:cancel_order:per_day']
const valid = (value: unknown): value is [string, string] => Array.isArray(value) && value.length === 2 && value.every((key) => typeof key === 'string' && /^account:[\w-]+:per_day$/.test(key)) && value[0] !== value[1]
export function readControlPreferences(accountId: number): [string, string] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(`axile:control:${location.origin}:${accountId}`) ?? 'null')
    return valid(value) ? value : [...DEFAULT_CONTROL_METRICS]
  } catch { return [...DEFAULT_CONTROL_METRICS] }
}
export function saveControlPreferences(accountId: number, value: [string, string]) {
  if (!valid(value)) return
  try { localStorage.setItem(`axile:control:${location.origin}:${accountId}`, JSON.stringify(value)) } catch { /* 当前会话仍生效。 */ }
}
