/**
 * 薄 API 客户端 —— 统一 baseURL 与错误归一。
 *
 * 后端仅绑定 loopback，不提供应用级鉴权；不得通过反向代理或端口转发暴露服务。
 * dev 下 `/api` 由 Vite 代理到 127.0.0.1:1419（见 vite.config.ts）。
 */

const BASE = '/api/v1'

/** API 错误保留完整响应；摘要由展示层单独生成。 */
export class ApiError extends Error {
  status: number
  code: string | null
  requestId: string | null
  rawText: string | null
  body: unknown
  validationIssues: unknown[]

  constructor(status: number, message: string, options: { code?: string | null; requestId?: string | null; rawText?: string | null; body?: unknown; validationIssues?: unknown[] } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = options.code ?? null
    this.requestId = options.requestId ?? null
    this.rawText = options.rawText ?? null
    this.body = options.body ?? null
    this.validationIssues = options.validationIssues ?? []
  }
}

type ErrorBody = {
  detail?: unknown
  message?: unknown
  code?: unknown
  request_id?: unknown
}

/** 全部字段消息进入正文，原始校验项另行完整保留。 */
function validationMessage(detail: unknown): string | null {
  if (!Array.isArray(detail)) return null
  const messages = detail.flatMap((issue) => {
    if (!issue || typeof issue !== 'object') return []
    const { loc, msg } = issue as { loc?: unknown; msg?: unknown }
    if (typeof msg !== 'string') return []
    const path = Array.isArray(loc) ? (loc[0] === 'body' ? loc.slice(1) : loc).map(String).join('.') : ''
    return [path ? `${path}：${msg}` : msg]
  })
  return messages.length ? messages.join('\n') : null
}

export function apiErrorFromBody(status: number, statusText: string, body: unknown, rawText: string | null = null, requestId: string | null = null): ApiError {
  const payload = body && typeof body === 'object' ? body as ErrorBody : {}
  const detail = payload.detail && typeof payload.detail === 'object' ? payload.detail as ErrorBody : {}
  const textMessage = (typeof body === 'string' ? body : rawText) || statusText
  const message = typeof payload.message === 'string'
    ? payload.message
    : typeof payload.detail === 'string'
      ? payload.detail
      : typeof detail.message === 'string' ? detail.message
        : (validationMessage(payload.detail) ?? textMessage) || `HTTP ${status}`
  return new ApiError(status, message, {
    code: typeof payload.code === 'string' ? payload.code : typeof detail.code === 'string' ? detail.code : null,
    requestId: typeof payload.request_id === 'string' ? payload.request_id : typeof detail.request_id === 'string' ? detail.request_id : requestId,
    body, rawText,
    validationIssues: Array.isArray(payload.detail) ? payload.detail : [],
  })
}

function headers(extra?: HeadersInit): HeadersInit {
  return { 'Content-Type': 'application/json', ...(extra as Record<string, string>) }
}

async function parse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const rawText = await res.text()
    let body: unknown = null
    try {
      body = JSON.parse(rawText)
    } catch {
      // 非 JSON 正文也属于错误原文。
    }
    throw apiErrorFromBody(res.status, res.statusText, body, rawText, res.headers.get('x-request-id'))
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

/** 发起 GET 请求。`signal` 用于配合轮询取消。 */
export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(BASE + path, { headers: headers(), signal })
  return parse<T>(res)
}

/** 发起带 JSON body 的写请求（POST/PATCH/DELETE）。 */
export async function apiSend<T>(
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const res = await fetch(BASE + path, {
    method,
    headers: headers(),
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })
  return parse<T>(res)
}

/** 上传 multipart 文件；浏览器负责生成 Content-Type boundary。 */
export async function apiUpload<T>(path: string, file: File): Promise<T> {
  const body = new FormData()
  body.append('file', file)
  const res = await fetch(BASE + path, { method: 'POST', body })
  return parse<T>(res)
}
