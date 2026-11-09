export type DurationParts = { hours: string; minutes: string; seconds: string }

/** 展示省略零单位；编辑时保留所有分段，全零仍有秒这一处入口。 */
export function durationPartVisible(parts: DurationParts, key: keyof DurationParts, editing = false): boolean {
  return editing || parts[key].trim() === '' || Number(parts[key]) !== 0
    || (key === 'seconds' && Object.values(parts).every(part => Number(part) === 0))
}

/** 分段编辑使用字符串，空段或非法草稿不能被当成零保存。 */
export function joinDuration(parts: DurationParts): string {
  if (!Object.values(parts).every(part => /^\d+$/.test(part))) return 'NaN'
  const total = Number(parts.hours) * 3600 + Number(parts.minutes) * 60 + Number(parts.seconds)
  return Number.isSafeInteger(total) ? String(total) : 'NaN'
}

export function splitDuration(value: string, showHours: boolean): DurationParts {
  const total = Number(value)
  if (value.trim() === '' || !Number.isSafeInteger(total) || total < 0)
    return { hours: '0', minutes: '0', seconds: value }
  return {
    hours: String(showHours ? Math.floor(total / 3600) : 0),
    minutes: String(showHours ? Math.floor(total / 60) % 60 : Math.floor(total / 60)),
    seconds: String(total % 60),
  }
}

/** 摘要省略零单位，接口仍使用总秒数。 */
export function formatDuration(total: number): string {
  const { hours, minutes, seconds } = splitDuration(String(total), true)
  return [Number(hours) ? `${hours} 时` : '', Number(minutes) ? `${minutes} 分` : '',
    Number(seconds) || total === 0 ? `${seconds} 秒` : ''].filter(Boolean).join(' ')
}
