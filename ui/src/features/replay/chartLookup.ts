/** 已按观察时间排序的记录中，第一个晚于 timeUs 的位置；同微秒保留最后一次观察。 */
export function replayTimeUpperBound(records: readonly { time_us: number }[], timeUs: number): number {
  let left = 0, right = records.length
  while (left < right) {
    const middle = left + Math.floor((right - left) / 2)
    if (records[middle].time_us <= timeUs) left = middle + 1
    else right = middle
  }
  return left
}

/** 拖动只提交每帧最后位置，结束时同步提交；切换上下文时丢弃旧位置。 */
export function replaySeekScheduler(seek: (timeUs: number) => void, request: (callback: FrameRequestCallback) => number, cancel: (frame: number) => void) {
  let frame: number | null = null, pending: number | null = null
  const clearFrame = () => { if (frame != null) cancel(frame); frame = null }
  const flush = () => {
    clearFrame()
    const time = pending
    pending = null
    if (time != null) seek(time)
  }
  return {
    schedule(timeUs: number) {
      pending = timeUs
      frame ??= request(() => { frame = null; flush() })
    },
    flush,
    cancel() { clearFrame(); pending = null },
  }
}
