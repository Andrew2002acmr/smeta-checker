import { createError, defineEventHandler, readBody, setHeader } from 'h3'
import { loginSession, logoutSession, requireSession } from '../utils/access'

export default defineEventHandler(async (event) => {
  setHeader(event, 'Cache-Control', 'no-store')
  if (event.method === 'GET') {
    // Status probe only: neither authentication mode creates sessions on GET.
    await requireSession(event)
    return { connected: true as const }
  }
  if (event.method === 'DELETE') {
    await logoutSession(event)
    return { connected: false as const }
  }
  if (event.method !== 'POST') throw createError({ statusCode: 405 })
  const body: { login?: unknown; password?: unknown } =
    ((await readBody(event).catch(() => ({}))) as Record<string, unknown>) || {}
  const login = typeof body.login === 'string' ? body.login : ''
  const password = typeof body.password === 'string' ? body.password : ''
  await loginSession(event, login, password)
  return { connected: true as const }
})
