/** 沿用参考组件的「常用规则 → 日期 + 时刻」描述方式，仅用于展示。 */
const COMMON_DESCRIPTIONS: Record<string, string> = {
  '* * * * *': '每分钟执行一次',
  '0 * * * *': '每小时整点执行',
  '*/5 * * * *': '每5分钟执行一次',
  '*/15 * * * *': '每15分钟执行一次',
  '*/30 * * * *': '每30分钟执行一次',
  '0 0 1 */3 *': '每季度第一天0点执行',
  '0 0 1 1,4,7,10 *': '每季度第一天0点执行',
  '0 0 1 1 *': '每年1月1日0点执行',
  '0-59 0-1 * * *': '每天0点至1点59分，每分钟执行一次',
}

const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

/** 展开有界字段供描述列表使用；保留原表达式，绝不反写或重编排程。 */
function fieldValues(expression: string, min: number, max: number): number[] {
  const values = new Set<number>()
  for (const part of expression.split(',')) {
    const match = /^(\*|\d+)(?:-(\d+))?(?:\/(\d+))?$/.exec(part)
    if (!match) continue
    const start = match[1] === '*' ? min : Number(match[1])
    const end = match[2] !== undefined ? Number(match[2]) : match[1] === '*' || match[3] !== undefined ? max : start
    const step = Number(match[3] ?? 1)
    if (start < min || end > max || step < 1 || start > end) continue
    for (let value = start; value <= end; value += step) values.add(value)
  }
  return [...values].sort((a, b) => a - b)
}

/** 连续三个及以上值合为范围，避免长列表重复单位；非连续值仍逐一列出。 */
function describeValues(values: number[], label: (value: number) => string): string {
  const parts: string[] = []
  for (let index = 0; index < values.length; index++) {
    const start = values[index]!
    let endIndex = index
    while (endIndex + 1 < values.length && values[endIndex + 1] === values[endIndex]! + 1) endIndex++
    if (endIndex - index >= 2) {
      parts.push(`${label(start)}至${label(values[endIndex]!)}`)
      index = endIndex
    } else {
      parts.push(label(start))
    }
  }
  return parts.join('、')
}

function fieldInterval(expression: string, min: number, max: number) {
  const match = /^(\*|\d+)(?:-(\d+))?\/(\d+)$/.exec(expression)
  return match ? {
    start: match[1] === '*' ? min : Number(match[1]),
    end: match[2] !== undefined ? Number(match[2]) : max,
    step: Number(match[3]),
  } : null
}

function describeCalendar(day: string, month: string, week: string): string {
  const months = fieldValues(month, 1, 12)
  const days = fieldValues(day, 1, 31)
  const weekdays = fieldValues(week, 0, 6)
  const monthText = months.length === 12 ? '' : `每年${describeValues(months, (value) => `${value}月`)}`
  const weekText = weekdays.length === 7 ? '' : describeValues(weekdays, (value) => WEEKDAYS[value]!)
  const everyDay = days.length === 31
  if (everyDay) {
    const repeat = weekText ? `每${weekText}` : '每天'
    return monthText ? `${monthText}，${repeat}` : repeat
  }

  let dayText: string
  const interval = fieldInterval(day, 1, 31)
  if (interval) {
    const range = interval.end === 31 ? `从${interval.start}日起` : `在${interval.start}日至${interval.end}日期间`
    dayText = `${range}，每隔${interval.step}天`
  } else {
    const labels = [describeValues(days, (value) => `${value}日`)]
    if (day.toLowerCase().split(',').includes('last')) labels.push('最后一天')
    dayText = labels.filter(Boolean).join('、')
  }
  const date = monthText ? `${monthText}的${interval ? '每个月' : ''}${dayText}` : `每月${dayText}`
  // APScheduler 的日期和星期是交集；参考组件中的「或」不适用于 axile。
  return weekText ? `${date}（且为${weekText}）` : date
}

function describeTime(minute: string, hour: string): string {
  const minutes = fieldValues(minute, 0, 59)
  const hours = fieldValues(hour, 0, 23)
  if (minutes.length === 1 && hours.length === 1) {
    return `${hours[0]}点${minutes[0] === 0 ? '整' : `${minutes[0]}分`}执行`
  }

  const everyHour = hours.length === 24
  const hourText = everyHour ? '每小时' : `${describeValues(hours, (value) => `${value}点`)}${hours.length > 1 ? '各小时' : ''}`
  const hourStep = fieldInterval(hour, 0, 23)
  if (minutes.length === 1 && minutes[0] === 0) {
    if (hourStep && hourStep.step > 1) {
      const range = hourStep.end === 23 ? `从${hourStep.start}点开始` : `在${hourStep.start}点至${hourStep.end}点之间`
      return `${range}，每隔${hourStep.step}小时整点执行`
    }
    return `${hourText}整点执行`
  }
  if (minutes.length === 60) return everyHour ? '每分钟执行一次' : `${hourText}，每分钟执行一次`

  const minuteStep = fieldInterval(minute, 0, 59)
  if (minuteStep) {
    const range = minuteStep.end === 59 ? `从${minuteStep.start}分开始` : `在${minuteStep.start}分至${minuteStep.end}分之间`
    return `${hourText}，${range}，每隔${minuteStep.step}分钟执行一次`
  }
  if (/^\d+-\d+$/.test(minute)) {
    return `${hourText}的${minutes[0]}分至${minutes.at(-1)}分，每分钟执行一次`
  }
  return `${hourText}的第${describeValues(minutes, String)}分钟执行`
}

/** 输入须先经过编辑器校验，月份、星期名称由调用方转换为 APScheduler 数字。 */
export function getCronDescription(expression: string): string {
  const parts = expression.trim().split(/\s+/)
  if (parts.length !== 5) return '无效的 CRON 表达式'
  const common = COMMON_DESCRIPTIONS[parts.join(' ')]
  if (common) return common
  const [minute, hour, day, month, week] = parts as [string, string, string, string, string]
  const calendar = describeCalendar(day, month, week)
  const time = describeTime(minute, hour)
  const separator = /^(每|从|在)/.test(time) || calendar.includes('每隔') ? '，' : ''
  return `${calendar}${separator}${time}`
}
