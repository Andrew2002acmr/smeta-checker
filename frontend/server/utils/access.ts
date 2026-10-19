import { createHash, randomBytes, timingSafeEqual } from 'node:crypto'
import type { H3Event } from 'h3'
const localSecret = randomBytes(32)
export function assertLocal(event: H3Event) {
  const address = event.node.req.socket.remoteAddress
  if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address || ''))
    throw createError({ statusCode: 403, statusMessage: 'Доступен только локальный стенд' })
  const url = getRequestURL(event)
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname))
    throw createError({ statusCode: 403, statusMessage: 'Недопустимый адрес' })
  if (!['GET', 'HEAD'].includes(event.method) && getHeader(event, 'origin') !== url.origin)
    throw createError({ statusCode: 403, statusMessage: 'Запрос должен поступить из приложения' })
}
function signature(value: string) {
  return createHash('sha256').update(localSecret).update(value).digest('hex')
}
export function issueLocalSession(event: H3Event) {
  assertLocal(event)
  const value = String(Date.now() + 86400000) + '.' + randomBytes(24).toString('hex')
  setCookie(event, 'kdfp_workspace', value + '.' + signature(value), {
    httpOnly: true,
    sameSite: 'strict',
    path: '/',
    maxAge: 86400,
    secure: getRequestURL(event).protocol === 'https:',
  })
  setHeader(event, 'Cache-Control', 'no-store')
}
export function requireSession(event: H3Event) {
  assertLocal(event)
  const cookie = getCookie(event, 'kdfp_workspace') || ''
  const parts = cookie.split('.')
  const received = Buffer.from(parts[2] || ''),
    expected = Buffer.from(signature(parts.slice(0, 2).join('.')))
  if (
    parts.length !== 3 ||
    Number(parts[0]) < Date.now() ||
    received.length !== expected.length ||
    !timingSafeEqual(received, expected)
  )
    throw createError({ statusCode: 401, statusMessage: 'Обновите подключение' })
}
