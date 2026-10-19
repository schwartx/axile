import { describe, expect, it } from 'bun:test'

import {
  OVERFLOW_TEXT_MIN_DURATION_MS,
  OVERFLOW_TEXT_SPEED_PX_PER_SECOND,
  overflowTextDistance,
  overflowTextDuration,
} from './overflowTextMotion'

describe('overflowTextDistance', () => {
  it('忽略一像素以内的布局测量误差', () => {
    expect(overflowTextDistance(200, 200)).toBe(0)
    expect(overflowTextDistance(200.75, 200)).toBe(0)
  })

  it('真实溢出向上取整，保证末字完整进入视口', () => {
    expect(overflowTextDistance(240.2, 200)).toBe(41)
  })
})

describe('overflowTextDuration', () => {
  it('不溢出或仅有测量误差时不播放', () => {
    expect(overflowTextDuration(0)).toBe(0)
    expect(overflowTextDuration(1)).toBe(0)
  })

  it('短距离使用最小时长，避免一闪而过', () => {
    expect(overflowTextDuration(2)).toBe(OVERFLOW_TEXT_MIN_DURATION_MS)
    expect(overflowTextDuration(20)).toBe(OVERFLOW_TEXT_MIN_DURATION_MS)
  })

  it('长距离按固定速度播放', () => {
    expect(overflowTextDuration(80)).toBe(1333)
    expect(overflowTextDuration(80)).toBe(
      Math.round((80 / OVERFLOW_TEXT_SPEED_PX_PER_SECOND) * 1000),
    )
  })
})
