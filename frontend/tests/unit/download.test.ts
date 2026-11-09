import { describe, expect, it, vi } from 'vitest'
import { useBackend } from '../../app/composables/useBackend'

interface DownloadClient {
  download: (url: string, filename: string) => Promise<void>
  downloadPost: (url: string, form: FormData, filename: string) => Promise<void>
}

function createClient(): DownloadClient {
  vi.stubGlobal('useRoute', () => ({ path: '/', fullPath: '/' }))
  vi.stubGlobal('useState', () => ({ value: false }))
  return useBackend() as unknown as DownloadClient
}

describe('download error text', () => {
  it('reports a readable error when a file download fails with non-JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        const body = '<html><body><h1>502 Bad Gateway</h1></body></html>'
        return new Response(body, {
          status: 502,
          headers: { 'Content-Type': 'text/html' },
        })
      }),
    )
    try {
      const backend = createClient()
      await expect(backend.download('/api/v1/audits/x/reports/word', 'audit.docx')).rejects.toThrow(
        'Запрос не выполнен',
      )
      await expect(
        backend.downloadPost('/api/v1/format-word', new FormData(), 'report.docx'),
      ).rejects.toThrow('Запрос не выполнен')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('reports the BFF json envelope when a file download fails with JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        return Response.json(
          { statusMessage: 'Сервер проверки недоступен. Повторите запрос.' },
          { status: 502 },
        )
      }),
    )
    try {
      const backend = createClient()
      await expect(backend.download('/api/v1/audits/x/reports/word', 'audit.docx')).rejects.toThrow(
        'Сервер проверки недоступен. Повторите запрос.',
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
