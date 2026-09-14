/**
 * The REST half of the client.
 *
 * One place decides what an error means, because the same three cases recur everywhere:
 * a 401 must send the user to the login page, a typed error must reach the caller with the
 * server's own message, and a successful empty body must not explode.
 *
 * `credentials: 'same-origin'` is load-bearing: the session lives in an HttpOnly cookie, so
 * a request without it is anonymous even while the user is looking at their own session.
 */

import { resolveApiBase } from './endpoints'

/** The session is missing, expired or revoked. The caller should show the login page. */
export class UnauthorizedError extends Error {
  constructor() {
    super('unauthenticated')
    this.name = 'UnauthorizedError'
  }
}

/** The request was understood and refused, with a message worth showing verbatim. */
export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return { error: text }
  }
}

/** Pull a human-readable message out of FastAPI's several error shapes. */
function describe(payload: unknown, status: number): string {
  const record = payload as { error?: unknown; detail?: unknown } | null
  const detail = record?.detail
  if (typeof record?.error === 'string') return record.error
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const nested = (detail as { error?: unknown }).error
    if (typeof nested === 'string') return nested
  }
  return `请求失败（HTTP ${status}）`
}

export interface RequestOptions {
  method?: string
  /** Serialised as JSON. Mutually exclusive with `raw`. */
  body?: unknown
  /** Sent as-is, for uploads and other non-JSON payloads. */
  raw?: BodyInit
  headers?: Record<string, string>
  signal?: AbortSignal
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const init: RequestInit = {
    method: options.method ?? 'GET',
    credentials: 'same-origin',
    signal: options.signal,
  }
  if (options.raw !== undefined) {
    init.body = options.raw
    if (options.headers) init.headers = options.headers
  } else if (options.body !== undefined) {
    init.body = JSON.stringify(options.body)
    init.headers = { 'content-type': 'application/json', ...(options.headers ?? {}) }
  } else if (options.headers) {
    init.headers = options.headers
  }

  const response = await fetch(`${resolveApiBase()}${path}`, init)
  if (response.status === 401) throw new UnauthorizedError()
  const payload = await readJson(response)
  if (!response.ok) throw new ApiError(describe(payload, response.status), response.status)
  return payload as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  /** Upload raw bytes; the filename lives in the path, so no multipart is involved. */
  upload: <T>(path: string, data: Blob | ArrayBuffer) =>
    request<T>(path, {
      method: 'PUT',
      raw: data,
      headers: { 'content-type': 'application/octet-stream' },
    }),
}

/** Percent-encode a workspace path for use in a query string. */
export function withPath(path: string): string {
  return `?path=${encodeURIComponent(path)}`
}

/**
 * A URL the browser can navigate to directly.
 *
 * Downloads go through the browser rather than `fetch` so the server can stream the file
 * and the browser keeps its own progress, resume and "save as" behaviour. The session
 * cookie rides along because it is a same-origin navigation.
 */
export function resolveDownloadUrl(path: string): string {
  return `${resolveApiBase()}/api/workspace/download${withPath(path)}`
}
