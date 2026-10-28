// Keep state/decoder tests independent of OPFS; the browser E2E runs the real worker.
class TestWorker {
  onmessage?: (event: { data: unknown }) => void
  postMessage({ id, accountId, command, query }: { id: number; accountId: number; command: string; query: Record<string, string> }) {
    if (command === 'pause') return
    const search = new URLSearchParams(query).toString()
    const path = `/api/v1/account/performance/${accountId}/${command}${search ? `?${search}` : ''}`
    void fetch(path, command === 'refresh' ? { method: 'POST' } : undefined).then(async response => {
      const raw = await response.text()
      let body: any
      try { body = JSON.parse(raw) } catch { body = { detail: raw } }
      this.onmessage?.({ data: response.ok ? { id, value: body } : { id, error: { status: response.status, message: body.detail?.message ?? body.detail ?? '', code: body.detail?.code } } })
    }).catch(error => this.onmessage?.({ data: { id, error: { message: error.message } } }))
  }
}
globalThis.Worker = TestWorker as unknown as typeof Worker
