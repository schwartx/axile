/** 恢复通知只供读取订阅；写操作、重算和任务创建不能注册在这里。 */
const readers = new Set<() => void | Promise<unknown>>()

export function subscribeReadRecovery(reader: () => void | Promise<unknown>): () => void {
  readers.add(reader)
  return () => { readers.delete(reader) }
}

/** 各读取者自行保存错误；一个读取失败不阻断其他区块恢复。 */
export async function notifyReadRecovery(): Promise<void> {
  await Promise.allSettled([...readers].map(async (reader) => { if (readers.has(reader)) await reader() }))
}
