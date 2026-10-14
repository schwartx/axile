import { connectionValidationApi, validationRunning, type ConnectionValidationJob } from '@/lib/api/connectionValidation'

/** 一次表单快照对应一次验证；离开页面后到达的 POST 也必须取消，不能留下孤立连接。 */
export class ConnectionValidationSession {
  private disposed = false
  private id: string | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private reading: AbortController | null = null
  private active = false

  constructor(
    private readonly onChange: (job: ConnectionValidationJob) => void,
    private readonly onError: (error: Error) => void,
    private readonly api = connectionValidationApi,
    private readonly interval = 1000,
  ) {}

  async start(channel: string, config: Record<string, unknown>): Promise<void> {
    try {
      const job = await this.api.create(channel, config)
      this.id = job.validation_id
      this.active = validationRunning(job)
      if (this.disposed) {
        if (this.active) await this.api.cancel(this.id)
        return
      }
      this.accept(job)
    } catch (error) {
      this.fail(error)
    }
  }

  private accept(job: ConnectionValidationJob): void {
    if (this.disposed) return
    this.active = validationRunning(job)
    this.onChange(job)
    if (this.active) this.timer = setTimeout(() => { void this.poll() }, this.interval)
  }

  private async poll(): Promise<void> {
    if (this.disposed || !this.id) return
    this.reading = new AbortController()
    try {
      this.accept(await this.api.get(this.id, this.reading.signal))
    } catch (error) {
      if (!this.reading.signal.aborted) this.fail(error)
    }
  }

  private fail(error: unknown): void {
    if (this.disposed) return
    this.stopPolling()
    this.onError(error instanceof Error ? error : new Error(String(error)))
  }

  async refresh(): Promise<void> {
    this.stopPolling()
    await this.poll()
  }

  async cancel(): Promise<void> {
    this.stopPolling()
    if (!this.id || this.disposed) return
    try {
      this.accept(await this.api.cancel(this.id))
    } catch (error) {
      this.fail(error)
    }
  }

  private stopPolling(): void {
    if (this.timer !== null) clearTimeout(this.timer)
    this.timer = null
    this.reading?.abort()
  }

  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.stopPolling()
    if (this.id && this.active) void this.api.cancel(this.id).catch(() => {})
  }
}
