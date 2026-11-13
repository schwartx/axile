export const AMOUNT_PATTERN = '(?:[0-9]+(?:\\.[0-9]+)?|\\.[0-9]+)'
const DECIMAL_AMOUNT = new RegExp(`^${AMOUNT_PATTERN}$`)
const MAGNITUDES = [[8, '亿'], [4, '万'], [3, '千'], [2, '百'], [0, '']] as const

/** 用十进制字符串移位，量级提示不舍入用户输入的金额。 */
export function amountMagnitude(value: string): string {
  if (value.trim() !== value || !DECIMAL_AMOUNT.test(value) || !Number.isFinite(Number(value))) return ''
  const [whole, fraction = ''] = value.split('.')
  const integer = whole.replace(/^0+/, '') || '0'
  const [places, unit] = MAGNITUDES.find(([places]) => integer.length > places)!
  const split = integer.length - places
  const decimal = (integer.slice(split) + fraction).replace(/0+$/, '')
  return `${integer.slice(0, split)}${decimal ? `.${decimal}` : ''}${unit}`
}
