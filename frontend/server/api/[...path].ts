import { createError, defineEventHandler, proxyRequest, setHeader } from 'h3'
import { requireSession } from '../utils/access'

export default defineEventHandler(async (event) => {
  const account = await requireSession(event)
  const config = useRuntimeConfig(event)
  if (!config.backendToken)
    throw createError({ statusCode: 503, statusMessage: 'Сервер проверки не подключён' })
  if (
    !/^\/api\/v1\/(projects|documents|audits|format-checks|format-report|format-word)(?:[/?]|$)/.test(
      event.path,
    )
  )
    throw createError({ statusCode: 404, statusMessage: 'Маршрут не найден' })
  const target = new URL(event.path, config.backendUrl)
  const backend = new URL(config.backendUrl)
  if (target.origin !== backend.origin) throw createError({ statusCode: 400 })
  setHeader(event, 'Cache-Control', 'no-store')
  try {
    return await proxyRequest(event, target.href, {
      headers: {
        authorization: 'Bearer ' + config.backendToken,
        'x-workspace-user': encodeURIComponent(account || 'api'),
        cookie: '',
        origin: backend.origin,
      },
      fetchOptions: { redirect: 'manual' },
    })
  } catch {
    throw createError({
      statusCode: 502,
      statusMessage: 'Сервер проверки недоступен. Повторите запрос.',
    })
  }
})
