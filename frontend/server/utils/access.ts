import { createHash, randomBytes, timingSafeEqual } from 'node:crypto'
import {
  createError,
  deleteCookie,
  getCookie,
  getHeader,
  getRequestURL,
  setCookie,
  setHeader,
} from 'h3'
import type { H3Event } from 'h3'
import {
  authenticateLdap,
  AuthDeniedError,
  checkLdapAccess,
  DirectoryUnavailableError,
} from './ldap'
import { getSessionStore, StoreUnavailableError } from './store'

export const SESSION_COOKIE = 'kdfp_workspace'
/** Opaque server-side session lifetime. */
export const SESSION_TTL_SEC = 8 * 3600
/** LDAP group membership recheck interval inside a live session. */
export const SESSION_RECHECK_SEC = 5 * 60
export const ACCOUNT_LIMIT = 3
export const ACCOUNT_WINDOW_SEC = 15 * 60
export const IP_LIMIT = 20
export const IP_WINDOW_SEC = 15 * 60

function publicHost(): string {
  return (process.env.NUXT_PUBLIC_HOST || '').trim().toLowerCase()
}

function localCredentials(): { login: string; password: string } {
  const login = (process.env.WORKSPACE_LOGIN || '').trim()
  const password = process.env.WORKSPACE_PASSWORD || ''
  if (!login || !password) unavailable()
  return { login, password }
}

/**
 * Empty (default) is local. Any other non-ldap value (e.g. a typo like
 * 'ldpa') fails closed with 503 instead of silently running local.
 */
export function authMode(): 'local' | 'ldap' {
  const raw = (process.env.WORKSPACE_AUTH_MODE || '').trim().toLowerCase()
  if (raw === '' || raw === 'local') return 'local'
  if (raw === 'ldap') return 'ldap'
  throw createError({ statusCode: 503, statusMessage: 'Вход временно недоступен' })
}

function ldapOriginConfig(): URL {
  let url: URL
  try {
    url = new URL((process.env.WORKSPACE_ORIGIN || '').trim())
  } catch {
    return unavailable()
  }
  if (
    url.protocol !== 'https:' ||
    url.username ||
    url.password ||
    url.pathname !== '/' ||
    url.search ||
    url.hash
  )
    return unavailable()
  return url
}

/**
 * - local (default): WORKSPACE_LOGIN + WORKSPACE_PASSWORD are required.
 *   Loopback/Host checks still restrict where the application is reachable;
 *   they never replace authentication, even on localhost or an intranet.
 * - ldap: AD login, Redis sessions, exact-host and exact-origin checks.
 *   LDAP without Redis fails closed.
 */
export function assertLocal(event: H3Event) {
  const host = publicHost()
  if (!host) {
    // Compose publishes the port on host loopback only; inside the container
    // the peer is the Docker gateway, not 127.0.0.1. NUXT_TRUST_PROXY=1 skips
    // the peer check but keeps Host allowlist, same-origin and session.
    // Never set it when the port is reachable from an untrusted network.
    // The socket address is authoritative: forwarded headers are never trusted.
    if (process.env.NUXT_TRUST_PROXY !== '1') {
      const address = event.node.req.socket.remoteAddress
      if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address || ''))
        throw createError({ statusCode: 403, statusMessage: 'Доступен только локальный стенд' })
    }
    const url = getRequestURL(event)
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname))
      throw createError({ statusCode: 403, statusMessage: 'Недопустимый адрес' })
    if (!['GET', 'HEAD'].includes(event.method) && getHeader(event, 'origin') !== url.origin)
      throw createError({ statusCode: 403, statusMessage: 'Запрос должен поступить из приложения' })
    return
  }
  const url = getRequestURL(event)
  if (url.hostname.toLowerCase() !== host)
    throw createError({ statusCode: 403, statusMessage: 'Недопустимый адрес' })
  if (!['GET', 'HEAD'].includes(event.method)) {
    const origin = getHeader(event, 'origin') || ''
    let originHost = ''
    try {
      originHost = new URL(origin).hostname.toLowerCase()
    } catch {
      throw createError({ statusCode: 403, statusMessage: 'Запрос должен поступить из приложения' })
    }
    if (originHost !== host)
      throw createError({ statusCode: 403, statusMessage: 'Запрос должен поступить из приложения' })
  }
}

/**
 * LDAP request gate. Every request (including GET downloads, navigation and
 * SSE, which carry no Origin) must present the exact configured raw Host
 * including port. State-changing requests must additionally present the exact
 * configured https origin. The Host is read from the raw Host header only;
 * X-Forwarded-* is never trusted.
 */
export function assertLdapRequest(event: H3Event) {
  const expected = ldapOriginConfig()
  const host = (event.node.req.headers.host || '').trim().toLowerCase()
  if (!host || host !== expected.host.toLowerCase())
    throw createError({ statusCode: 403, statusMessage: 'Недопустимый адрес' })
  if (!['GET', 'HEAD'].includes(event.method) && getHeader(event, 'origin') !== expected.origin)
    throw createError({ statusCode: 403, statusMessage: 'Запрос должен поступить из приложения' })
}

function peerIp(event: H3Event): string {
  return event.node.req.socket.remoteAddress || 'unknown'
}

function genericAuthFailure(): never {
  // Same message for bad credentials, unknown users, locked groups and rate
  // limits: never signal which one fired, never leak directory errors.
  throw createError({ statusCode: 401, statusMessage: 'Неверный логин или пароль' })
}

function unavailable(): never {
  throw createError({ statusCode: 503, statusMessage: 'Вход временно недоступен' })
}

function localCredentialsMatch(
  login: string,
  password: string,
  expected: { login: string; password: string },
): boolean {
  const loginMatches = timingSafeEqual(
    createHash('sha256').update(login.trim()).digest(),
    createHash('sha256').update(expected.login).digest(),
  )
  const passwordMatches = timingSafeEqual(
    createHash('sha256').update(password).digest(),
    createHash('sha256').update(expected.password).digest(),
  )
  return loginMatches && passwordMatches
}

/** Seconds left before the absolute created+TTL deadline. */
function remainingTtlSec(created: number): number {
  return SESSION_TTL_SEC - Math.floor((Date.now() - created) / 1000)
}

async function createSession(
  event: H3Event,
  account: string,
  dn: string,
  mode: 'local' | 'ldap',
): Promise<void> {
  const id = randomBytes(32).toString('hex')
  const store = getSessionStore()
  const now = Date.now()
  try {
    const ug = await store.userGen(account)
    const ag = await store.globalGen()
    await store.write(
      id,
      { v: 1, id, account, dn, mode, ug, ag, created: now, checked: now },
      SESSION_TTL_SEC,
    )
  } catch (error) {
    if (error instanceof StoreUnavailableError) unavailable()
    throw error
  }
  setCookie(event, SESSION_COOKIE, id, {
    httpOnly: true,
    sameSite: 'strict' as const,
    path: '/',
    maxAge: SESSION_TTL_SEC,
    secure: mode === 'ldap' ? true : getRequestURL(event).protocol === 'https:',
  })
  setHeader(event, 'Cache-Control', 'no-store')
}

async function consumeRateLimit(key: string, windowSec: number, limit: number): Promise<void> {
  // Counters increment before any LDAP bind, single atomic INCR per key, no retries.
  let allowed: boolean
  try {
    allowed = await getSessionStore().hitRateLimit(key, windowSec, limit)
  } catch (error) {
    if (error instanceof StoreUnavailableError) unavailable()
    throw error
  }
  if (!allowed) genericAuthFailure()
}

/**
 * Canonical rate-limit key for one human: bare, domain\\user and user@domain
 * spellings share a single budget. Full domain validation stays in ldap.ts;
 * this only folds obvious aliases so the account limit cannot be sidestepped.
 */
function rateAccountKey(login: string): string {
  let name = String(login || '')
    .trim()
    .toLowerCase()
  const slash = name.lastIndexOf('\\')
  if (slash >= 0) name = name.slice(slash + 1)
  const at = name.indexOf('@')
  if (at >= 0) name = name.slice(0, at)
  name = name.replace(/[^a-z0-9._-]/g, '').slice(0, 64) || 'empty'
  return 'kdfp:rl:acct:' + name
}

async function requireServerSession(event: H3Event, mode: 'local' | 'ldap') {
  const id = getCookie(event, SESSION_COOKIE) || ''
  if (!/^[0-9a-f]{64}$/.test(id))
    throw createError({ statusCode: 401, statusMessage: 'Обновите подключение' })
  let rec
  try {
    rec = await getSessionStore().get(id)
  } catch (error) {
    if (error instanceof StoreUnavailableError) unavailable()
    throw error
  }
  if (!rec || rec.mode !== mode || (mode === 'local' && rec.account !== localCredentials().login)) {
    deleteCookie(event, SESSION_COOKIE, { path: '/' })
    throw createError({ statusCode: 401, statusMessage: 'Обновите подключение' })
  }
  if (remainingTtlSec(rec.created) <= 0) {
    try {
      await getSessionStore().remove(id)
    } catch {
      // Expiry cleanup is best-effort; the request is denied regardless.
    }
    deleteCookie(event, SESSION_COOKIE, { path: '/' })
    throw createError({ statusCode: 401, statusMessage: 'Обновите подключение' })
  }
  let ug: number
  let ag: number
  try {
    ;[ug, ag] = await Promise.all([
      getSessionStore().userGen(rec.account),
      getSessionStore().globalGen(),
    ])
  } catch (error) {
    if (error instanceof StoreUnavailableError) unavailable()
    throw error
  }
  if (ug !== rec.ug || ag !== rec.ag) {
    deleteCookie(event, SESSION_COOKIE, { path: '/' })
    throw createError({ statusCode: 401, statusMessage: 'Обновите подключение' })
  }
  if (mode === 'ldap' && Date.now() - rec.checked >= SESSION_RECHECK_SEC * 1000) {
    let ok: boolean
    try {
      ok = await checkLdapAccess(rec.dn)
    } catch (error) {
      // Fail closed: directory errors revoke nothing but deny the request.
      if (error instanceof DirectoryUnavailableError) unavailable()
      throw error
    }
    if (!ok) {
      try {
        await getSessionStore().remove(id)
      } catch {
        // Revocation is best-effort; the request is denied regardless.
      }
      deleteCookie(event, SESSION_COOKIE, { path: '/' })
      throw createError({ statusCode: 401, statusMessage: 'Обновите подключение' })
    }
    rec.checked = Date.now()
    try {
      // Refresh with the remaining TTL: the absolute created+TTL deadline
      // never extends, so sessions cannot live past 8h.
      if (!(await getSessionStore().refresh(id, rec, Math.max(1, remainingTtlSec(rec.created)))))
        throw createError({ statusCode: 401, statusMessage: 'Обновите подключение' })
    } catch (error) {
      if (error instanceof StoreUnavailableError) unavailable()
      throw error
    }
  }
  return rec.account
}

/**
 * Single session gate for every protected route. All modes use opaque
 * server-side sessions (Redis in LDAP mode and whenever WORKSPACE_REDIS_URL
 * is set, process memory for Redis-less local stands). There is no signed
 * cookie stack: any legacy cookie shape fails the id format check with 401.
 */
export async function requireSession(event: H3Event) {
  const mode = authMode()
  if (mode === 'ldap') {
    assertLdapRequest(event)
    return await requireServerSession(event, 'ldap')
  }
  assertLocal(event)
  localCredentials()
  return await requireServerSession(event, 'local')
}

/** POST /api/session: the selected local or AD credential check, never anonymous. */
export async function loginSession(event: H3Event, login = '', password = '') {
  const mode = authMode()
  if (mode === 'ldap') {
    assertLdapRequest(event)
    // IP budget first, then the normalized account budget: a blocked account
    // must not let an attacker skip the shared IP limit.
    await consumeRateLimit('kdfp:rl:ip:' + peerIp(event), IP_WINDOW_SEC, IP_LIMIT)
    await consumeRateLimit(rateAccountKey(login), ACCOUNT_WINDOW_SEC, ACCOUNT_LIMIT)
    try {
      // authenticateLdap already enforces group membership; no second check
      // here, so one login costs exactly one bind pair.
      const { dn, account } = await authenticateLdap(login, password)
      await createSession(event, account, dn, 'ldap')
    } catch (error) {
      if (error instanceof AuthDeniedError) genericAuthFailure()
      if (error instanceof DirectoryUnavailableError) unavailable()
      if (error instanceof StoreUnavailableError) unavailable()
      throw error
    }
    return
  }
  assertLocal(event)
  const expected = localCredentials()
  await consumeRateLimit('kdfp:rl:ip:' + peerIp(event), IP_WINDOW_SEC, IP_LIMIT)
  await consumeRateLimit(rateAccountKey(login), ACCOUNT_WINDOW_SEC, ACCOUNT_LIMIT)
  if (!login.trim() || !password || !localCredentialsMatch(login, password, expected))
    genericAuthFailure()
  await createSession(event, expected.login, '', 'local')
}

/** DELETE /api/session: destroys the server record and clears the cookie. */
export async function logoutSession(event: H3Event) {
  const mode = authMode()
  if (mode === 'ldap') assertLdapRequest(event)
  else assertLocal(event)
  const id = getCookie(event, SESSION_COOKIE) || ''
  if (/^[0-9a-f]{64}$/.test(id)) {
    try {
      await getSessionStore().remove(id)
    } catch (error) {
      if (error instanceof StoreUnavailableError) unavailable()
      throw error
    }
  }
  deleteCookie(event, SESSION_COOKIE, {
    path: '/',
    secure: mode === 'ldap',
    httpOnly: true,
    sameSite: 'strict',
  })
  setHeader(event, 'Cache-Control', 'no-store')
}

export function workspaceGate(): { locked: boolean; mode: 'local' | 'ldap' } {
  const mode = authMode()
  if (mode === 'local') localCredentials()
  return { locked: true, mode }
}
