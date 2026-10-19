import { z } from 'zod'
import {
  zProjectDto,
  zVersionDto,
  zRunDto,
  zSummaryDto,
  zRowsDto,
  zWorkspaceDto,
  zAuditCreateResponse,
  zPreflightResponse,
  zDocumentUploadResponse,
  zStructureDto,
  zFormatDto,
} from '~/generated/zod.gen'

export function readableError(error: unknown): string {
  if (error instanceof z.ZodError) return 'Формат ответа сервера изменился. Обновите приложение.'
  return error instanceof Error ? error.message : 'Не удалось выполнить действие'
}
export function backendError(value: unknown): string {
  const parsed = z
    .object({
      error: z.object({ message: z.string(), code: z.string().optional() }).optional(),
      detail: z.string().optional(),
      statusMessage: z.string().optional(),
      message: z.string().optional(),
    })
    .safeParse(value)
  return parsed.success
    ? parsed.data.error?.message ||
        parsed.data.detail ||
        parsed.data.statusMessage ||
        parsed.data.message ||
        'Запрос не выполнен'
    : 'Запрос не выполнен'
}
export function useBackend() {
  async function json<T>(url: string, schema: z.ZodType<T>, options: RequestInit = {}): Promise<T> {
    const headers = new Headers(options.headers)
    if (options.body && !(options.body instanceof FormData))
      headers.set('Content-Type', 'application/json')
    const response = await fetch(url, {
      ...options,
      headers,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: options.signal ?? AbortSignal.timeout(options.method === 'POST' ? 120000 : 15000),
    })
    const data: unknown = await response.json()
    if (!response.ok) throw Error(backendError(data))
    return schema.parse(data)
  }
  const root = '/api/v1'
  const audit = (id: string) => `${root}/audits/${encodeURIComponent(id)}`
  const contentUrl = (id: string) => `${root}/documents/${encodeURIComponent(id)}/content`
  async function download(url: string, filename: string) {
    const reply = await fetch(url, { credentials: 'same-origin' })
    if (!reply.ok) throw Error(backendError(await reply.json()))
    const href = URL.createObjectURL(await reply.blob())
    const link = document.createElement('a')
    link.href = href
    link.download = filename
    link.click()
    setTimeout(() => URL.revokeObjectURL(href), 1000)
  }
  return {
    json,
    audit,
    contentUrl,
    download,
    connect: () =>
      json('/api/session', z.object({ connected: z.literal(true) }), { method: 'POST' }),
    projects: () => json(root + '/projects', z.array(zProjectDto)),
    createProject: (name: string) =>
      json(root + '/projects', zProjectDto, { method: 'POST', body: JSON.stringify({ name }) }),
    documents: (id: string) =>
      json(`${root}/projects/${encodeURIComponent(id)}/documents`, z.array(zVersionDto)),
    upload: (form: FormData) =>
      json(root + '/documents', zDocumentUploadResponse, { method: 'POST', body: form }),
    preflight: (payload: unknown) =>
      json(root + '/documents/preflight', zPreflightResponse, {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    start: (payload: unknown) =>
      json(root + '/audits', zAuditCreateResponse, {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    runs: (id: string, offset = 0) =>
      json(
        `${root}/projects/${encodeURIComponent(id)}/audits?offset=${offset}&limit=50`,
        z.array(zRunDto),
      ),
    run: (id: string) => json(audit(id) + '/progress', zRunDto),
    summary: (id: string) => json(audit(id) + '/summary', zSummaryDto),
    rows: (id: string, query: { focus: string; q: string; offset: number }) =>
      json(
        audit(id) + '/rows?' + new URLSearchParams({ ...query, offset: String(query.offset) }),
        zRowsDto,
      ),
    workspace: (id: string, position: number) =>
      json(`${audit(id)}/positions/${position}/workspace`, zWorkspaceDto),
    structure: (id: string) =>
      json(`${root}/documents/${encodeURIComponent(id)}/structure`, zStructureDto),
    formatting: (id: string, sheet = '') =>
      json(
        `${root}/documents/${encodeURIComponent(id)}/format-checks?sheet=${encodeURIComponent(sheet)}`,
        zFormatDto,
        {
          method: 'POST',
        },
      ),
  }
}
