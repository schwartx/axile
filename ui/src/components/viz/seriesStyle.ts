/** 序列身份不随收益方向、排名或勾选顺序变化；线型补充颜色区分。 */
export function seriesStyle(key: string) {
  let hash = 0
  for (const char of key) hash = (Math.imul(hash, 31) + char.codePointAt(0)!) >>> 0
  return { color: `var(--color-chart-${hash % 5 + 1})`, dash: [[], [6, 3], [2, 3]][Math.floor(hash / 5) % 3] }
}
