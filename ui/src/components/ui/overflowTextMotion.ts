export const OVERFLOW_TEXT_DELAY_MS = 100
export const OVERFLOW_TEXT_MIN_DURATION_MS = 400
export const OVERFLOW_TEXT_SPEED_PX_PER_SECOND = 60

const OVERFLOW_THRESHOLD_PX = 1

/** 按溢出距离计算播放时长；短距离保留最小时长，长距离保持恒定阅读速度。 */
export function overflowTextDuration(distance: number): number {
  if (distance <= OVERFLOW_THRESHOLD_PX) return 0
  return Math.max(
    OVERFLOW_TEXT_MIN_DURATION_MS,
    Math.round((distance / OVERFLOW_TEXT_SPEED_PX_PER_SECOND) * 1000),
  )
}

/** 抹掉亚像素测量误差，只保留真正需要播放的整数位移。 */
export function overflowTextDistance(contentWidth: number, viewportWidth: number): number {
  const distance = Math.max(0, contentWidth - viewportWidth)
  return distance > OVERFLOW_THRESHOLD_PX ? Math.ceil(distance) : 0
}
