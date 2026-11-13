import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createEvent } from 'h3'
import type * as StoreModule from '../../server/utils/store'

const LDAP_PATH = '../../server/utils/ldap'
const STORE_PATH = '../../server/utils/store'
const ACCESS_PATH = '../../server/utils/access'

// Keep one module mock: queued doUnmock/doMock pairs can race during dynamic
// imports and occasionally expose the real LDAP client to a unit test.
const ldapMock = vi.hoisted(() => ({
  authenticate: undefined as
    | undefined
    | ((login: string, password: string) => Promise<{ dn: string; account: string }>),
  check: undefined as undefined | ((dn: string) => Promise<boolean>),
}))

vi.mock('../../server/utils/ldap', () => {
  class AuthDeniedError extends Error {
    constructor(message = 'denied') {
      super(message)
      this.name = 'AuthDeniedError'
    }
  }
  class DirectoryUnavailableError extends Error {
    constructor(message = 'unavailable') {
      super(message)
      this.name = 'DirectoryUnavailableError'
    }
  }
  return {
    AuthDeniedError,
    DirectoryUnavailableError,
    authenticateLdap: (login: string, password: string) =>
      ldapMock.authenticate?.(login, password) ??
      Promise.resolve({ dn: 'cn=test,dc=x', account: 'test' }),
    checkLdapAccess: (dn: string) => ldapMock.check?.(dn) ?? Promise.resolve(true),
  }
})

function mockLdap(
  overrides: {
    authenticate?: (login: string, password: string) => Promise<{ dn: string; account: string }>
    check?: (dn: string) => Promise<boolean>
  } = {},
) {
  ldapMock.authenticate = overrides.authenticate
  ldapMock.check = overrides.check
}

interface NodeRes {
  getHeader(name: string): string | string[] | undefined
  setHeader(name: string, value: string | string[]): void
  removeHeader(name: string): void
  appendHeader(name: string, value: string): void
}

function nodeRes(): NodeRes & { headers: Record<string, string | string[]> } {
  const headers: Record<string, string | string[]> = {}
  return {
    headers,
    getHeader: (name: string) => headers[name.toLowerCase()],
    setHeader: (name: string, value: string | string[]) => {
      headers[name.toLowerCase()] = value
    },
    removeHeader: (name: string) => {
      delete headers[name.toLowerCase()]
    },
    appendHeader: (name: string, value: string) => {
      const key = name.toLowerCase()
      const current = headers[key]
      headers[key] =
        current === undefined ? value : [...(Array.isArray(current) ? current : [current]), value]
    },
  }
}

function makeEvent(
  options: {
    method?: string
    url?: string
    host?: string
    headers?: Record<string, string>
    remoteAddress?: string
  } = {},
) {
  const res = nodeRes()
  const host = options.host ?? '127.0.0.1:3000'
  const req = {
    method: options.method ?? 'GET',
    url: options.url ?? '/api/session',
    headers: { host, origin: `http://${host}`, ...options.headers },
    socket: { remoteAddress: options.remoteAddress ?? '127.0.0.1' },
    connection: {},
  }
  return { event: createEvent(req as never, res as never), res }
}

function setCookies(res: NodeRes): string[] {
  const raw = res.getHeader('set-cookie')
  if (!raw) return []
  return Array.isArray(raw) ? raw : [raw]
}

function sessionCookie(res: NodeRes): string {
  const found = setCookies(res).find((c) => c.startsWith('kdfp_workspace='))
  if (!found) throw new Error('expected session cookie')
  return found.split(';', 1)[0].split('=', 2)[1]
}

/** LDAP tests speak raw Host (socket semantics); h3 getRequestURL trusts forwarded headers, we must not. */
const LDAP_HOST = 'audit.example.com'
const LDAP_HEADERS = { origin: 'https://audit.example.com' }

function ldapEvent(
  options: { method?: string; cookie?: string; headers?: Record<string, string> } = {},
) {
  const headers: Record<string, string> = { ...(options.headers ?? LDAP_HEADERS) }
  if (options.cookie) headers.cookie = `kdfp_workspace=${options.cookie}`
  return makeEvent({
    method: options.method ?? 'GET',
    url: '/api/session',
    host: LDAP_HOST,
    headers,
  })
}

function withEnv(env: Record<string, string | undefined>, run: () => Promise<void>) {
  return (async () => {
    const saved: Record<string, string | undefined> = {}
    for (const key of Object.keys(env)) {
      saved[key] = process.env[key]
      if (env[key] === undefined) delete process.env[key]
      else process.env[key] = env[key]
    }
    vi.resetModules()
    try {
      await run()
    } finally {
      for (const key of Object.keys(env)) {
        if (saved[key] === undefined) delete process.env[key]
        else process.env[key] = saved[key]
      }
      vi.resetModules()
    }
  })()
}

/**
 * LDAP-mode access under test. Production mandates Redis for LDAP, so the
 * tests borrow a real MemorySessionStore (acquired from the real store module
 * under a momentary local env) and mock getSessionStore to return it.
 * Production's mandatory-Redis path is untouched.
 */
async function ldapSetup(
  overrides: {
    authenticate?: (login: string, password: string) => Promise<{ dn: string; account: string }>
    check?: (dn: string) => Promise<boolean>
  } = {},
) {
  mockLdap(overrides)
  vi.resetModules()
  const savedMode = process.env.WORKSPACE_AUTH_MODE
  const savedUrl = process.env.WORKSPACE_REDIS_URL
  delete process.env.WORKSPACE_AUTH_MODE
  delete process.env.WORKSPACE_REDIS_URL
  const real = await vi.importActual<typeof StoreModule>(STORE_PATH)
  const mem = real.getSessionStore()
  if (savedMode === undefined) delete process.env.WORKSPACE_AUTH_MODE
  else process.env.WORKSPACE_AUTH_MODE = savedMode
  if (savedUrl === undefined) delete process.env.WORKSPACE_REDIS_URL
  else process.env.WORKSPACE_REDIS_URL = savedUrl
  vi.doMock(STORE_PATH, () => ({ ...real, getSessionStore: () => mem }))
  const access = await import(ACCESS_PATH)
  return { access, mem, real }
}

beforeEach(() => {
  vi.resetModules()
  mockLdap()
  vi.doUnmock(STORE_PATH)
})

describe('local login/password mode', () => {
  const LOCAL_ENV = {
    WORKSPACE_AUTH_MODE: 'local',
    WORKSPACE_LOGIN: 'WorkspaceUser',
    WORKSPACE_PASSWORD: ' s3cret ',
    WORKSPACE_REDIS_URL: undefined,
    NUXT_PUBLIC_HOST: undefined,
    NUXT_TRUST_PROXY: undefined,
  }

  it('authenticates without AD, keeps APIs behind the session, and revokes on logout', async () => {
    await withEnv({ ...LOCAL_ENV, LDAP_URL: 'invalid-directory-url' }, async () => {
      const access = await import(ACCESS_PATH)
      expect(access.workspaceGate()).toEqual({ locked: true, mode: 'local' })
      await expect(access.requireSession(makeEvent().event)).rejects.toMatchObject({
        statusCode: 401,
      })
      const created = makeEvent({ method: 'POST' })
      await access.loginSession(created.event, 'WorkspaceUser', ' s3cret ')
      const cookie = sessionCookie(created.res)
      expect(cookie).toMatch(/^[0-9a-f]{64}$/)
      const probe = makeEvent({ headers: { cookie: `kdfp_workspace=${cookie}` } })
      await access.requireSession(probe.event)
      const record = await (await import(STORE_PATH)).getSessionStore().get(cookie)
      expect(record?.account).toBe('WorkspaceUser')
      const logout = makeEvent({
        method: 'DELETE',
        headers: { cookie: `kdfp_workspace=${cookie}` },
      })
      await access.logoutSession(logout.event)
      await expect(access.requireSession(probe.event)).rejects.toMatchObject({ statusCode: 401 })
    })
  })

  it.each([
    ['wrong-user', ' s3cret '],
    ['workspaceuser', ' s3cret '],
    ['WorkspaceUser', 'wrong-password'],
    ['', ' s3cret '],
    ['WorkspaceUser', ''],
    ['WorkspaceUser', 's3cret'],
  ])(
    'rejects incorrect or empty credentials without issuing a cookie (%s)',
    async (login, password) => {
      await withEnv(LOCAL_ENV, async () => {
        const access = await import(ACCESS_PATH)
        const denied = makeEvent({ method: 'POST' })
        await expect(access.loginSession(denied.event, login, password)).rejects.toMatchObject({
          statusCode: 401,
        })
        expect(setCookies(denied.res)).toEqual([])
      })
    },
  )

  it.each([
    [undefined, undefined],
    ['WorkspaceUser', undefined],
    [undefined, 'password'],
    ['   ', 'password'],
  ])('fails closed when local credentials are not configured (%s)', async (login, password) => {
    await withEnv(
      { ...LOCAL_ENV, WORKSPACE_LOGIN: login, WORKSPACE_PASSWORD: password },
      async () => {
        const access = await import(ACCESS_PATH)
        expect(() => access.workspaceGate()).toThrowError(
          expect.objectContaining({ statusCode: 503 }),
        )
        const denied = makeEvent({ method: 'POST' })
        await expect(access.loginSession(denied.event, '', '')).rejects.toMatchObject({
          statusCode: 503,
        })
        expect(setCookies(denied.res)).toEqual([])
      },
    )
  })

  it('limits local attempts using the existing server-side account budget', async () => {
    await withEnv(LOCAL_ENV, async () => {
      const access = await import(ACCESS_PATH)
      for (let i = 0; i < 3; i++) {
        await expect(
          access.loginSession(makeEvent({ method: 'POST' }).event, 'WorkspaceUser', 'wrong'),
        ).rejects.toMatchObject({ statusCode: 401 })
      }
      const blocked = makeEvent({ method: 'POST' })
      await expect(
        access.loginSession(blocked.event, 'WorkspaceUser', ' s3cret '),
      ).rejects.toMatchObject({ statusCode: 401 })
      expect(setCookies(blocked.res)).toEqual([])
    })
  })

  it('expires sessions at the absolute deadline and rejects previous local identities', async () => {
    await withEnv(LOCAL_ENV, async () => {
      const access = await import(ACCESS_PATH)
      const store = (await import(STORE_PATH)).getSessionStore()
      const created = makeEvent({ method: 'POST' })
      await access.loginSession(created.event, 'WorkspaceUser', ' s3cret ')
      const cookie = sessionCookie(created.res)
      const rec = await store.get(cookie)
      if (!rec) throw new Error('Missing created session')
      await store.write(cookie, { ...rec, account: 'local' }, 3600)
      const probe = makeEvent({ headers: { cookie: `kdfp_workspace=${cookie}` } })
      await expect(access.requireSession(probe.event)).rejects.toMatchObject({ statusCode: 401 })
      await store.write(cookie, { ...rec, created: Date.now() - 8 * 3600 * 1000 - 1000 }, 3600)
      await expect(access.requireSession(probe.event)).rejects.toMatchObject({ statusCode: 401 })
    })
  })

  it('fails closed on unknown auth modes instead of falling back to local', async () => {
    await withEnv({ ...LOCAL_ENV, WORKSPACE_AUTH_MODE: 'ldpa' }, async () => {
      const access = await import(ACCESS_PATH)
      expect(() => access.authMode()).toThrowError(expect.objectContaining({ statusCode: 503 }))
      await expect(
        access.loginSession(makeEvent({ method: 'POST' }).event, 'WorkspaceUser', ' s3cret '),
      ).rejects.toMatchObject({ statusCode: 503 })
    })
  })
})

describe('LDAP session flow', () => {
  const LDAP_ENV = {
    WORKSPACE_AUTH_MODE: 'ldap',
    WORKSPACE_REDIS_URL: undefined,
    WORKSPACE_ORIGIN: 'https://audit.example.com',
    WORKSPACE_PASSWORD: undefined,
    NUXT_TRUST_PROXY: undefined,
  }

  it('logs in, reuses the session for GET without Origin, logs out, rejects replay', async () => {
    await withEnv(LDAP_ENV, async () => {
      const { access } = await ldapSetup()
      expect(access.workspaceGate()).toEqual({ locked: true, mode: 'ldap' })
      const login = ldapEvent({ method: 'POST' })
      await access.loginSession(login.event, 'alice', 'correct-horse')
      const cookie = sessionCookie(login.res)
      expect(cookie).toMatch(/^[0-9a-f]{64}$/)
      const header = setCookies(login.res).find((c) => c.startsWith('kdfp_workspace='))!
      expect(header).toMatch(/HttpOnly/i)
      expect(header).toMatch(/SameSite=Strict/i)
      expect(header).toMatch(/Secure/i)
      // GET carries no Origin (navigation/download/SSE): Host check suffices.
      await access.requireSession(ldapEvent({ cookie }).event)
      const logout = ldapEvent({ method: 'DELETE', cookie })
      await access.logoutSession(logout.event)
      await expect(access.requireSession(ldapEvent({ cookie }).event)).rejects.toMatchObject({
        statusCode: 401,
      })
    })
  })

  it('does not accept a directory session after switching to local mode', async () => {
    await withEnv(
      { ...LDAP_ENV, WORKSPACE_LOGIN: 'alice', WORKSPACE_PASSWORD: 'local-password' },
      async () => {
        const { access } = await ldapSetup()
        const created = ldapEvent({ method: 'POST' })
        await access.loginSession(created.event, 'alice', 'directory-password')
        const cookie = sessionCookie(created.res)
        process.env.WORKSPACE_AUTH_MODE = 'local'
        await expect(
          access.requireSession(
            makeEvent({ headers: { cookie: `kdfp_workspace=${cookie}` } }).event,
          ),
        ).rejects.toMatchObject({ statusCode: 401 })
      },
    )
  })

  it('returns generic 401 for bad credentials and never the directory message', async () => {
    await withEnv(LDAP_ENV, async () => {
      const { access } = await ldapSetup({
        authenticate: async () => {
          const { AuthDeniedError } = await import(LDAP_PATH)
          throw new AuthDeniedError('invalidCredentials (49)')
        },
      })
      const login = ldapEvent({ method: 'POST' })
      const failure = await access
        .loginSession(login.event, 'alice', 'wrong')
        .catch((e: unknown) => e)
      expect(failure).toMatchObject({ statusCode: 401 })
      expect(String((failure as Error).message)).toBe('Неверный логин или пароль')
      expect(String((failure as Error).message)).not.toContain('49')
    })
  })
  it('fails closed with 503 on directory outage and misconfigured origin', async () => {
    await withEnv(
      { ...LDAP_ENV, WORKSPACE_LOGIN: 'fallback-user', WORKSPACE_PASSWORD: 'fallback-password' },
      async () => {
        const { access } = await ldapSetup({
          authenticate: async () => {
            const { DirectoryUnavailableError } = await import(LDAP_PATH)
            throw new DirectoryUnavailableError('connect ETIMEDOUT')
          },
        })
        await expect(
          access.loginSession(
            ldapEvent({ method: 'POST' }).event,
            'fallback-user',
            'fallback-password',
          ),
        ).rejects.toMatchObject({ statusCode: 503 })
      },
    )
    await withEnv({ ...LDAP_ENV, WORKSPACE_ORIGIN: 'http://audit.example.com' }, async () => {
      const { access } = await ldapSetup()
      await expect(
        access.loginSession(ldapEvent({ method: 'POST' }).event, 'alice', 'pw'),
      ).rejects.toMatchObject({ statusCode: 503 })
    })
    await withEnv({ ...LDAP_ENV, WORKSPACE_ORIGIN: 'https://user@audit.example.com' }, async () => {
      const { access } = await ldapSetup()
      await expect(
        access.loginSession(ldapEvent({ method: 'POST' }).event, 'alice', 'pw'),
      ).rejects.toMatchObject({ statusCode: 503 })
    })
  })

  it('checks exact raw Host on every request, Origin only on mutations', async () => {
    await withEnv(LDAP_ENV, async () => {
      const { access } = await ldapSetup()
      const login = ldapEvent({ method: 'POST' })
      await access.loginSession(login.event, 'alice', 'pw')
      const cookie = sessionCookie(login.res)
      // Wrong Host fails even for GET.
      await expect(
        access.requireSession(
          makeEvent({ host: 'evil.test', headers: { cookie: `kdfp_workspace=${cookie}` } }).event,
        ),
      ).rejects.toMatchObject({ statusCode: 403 })
      // Right Host + no Origin passes for GET.
      await access.requireSession(ldapEvent({ cookie, headers: {} }).event)
      // Mutations require the exact origin.
      await expect(
        access.logoutSession(ldapEvent({ method: 'DELETE', cookie, headers: {} }).event),
      ).rejects.toMatchObject({ statusCode: 403 })
      await expect(
        access.loginSession(
          ldapEvent({ method: 'POST', headers: { origin: 'https://evil.test' } }).event,
          'alice',
          'pw',
        ),
      ).rejects.toMatchObject({ statusCode: 403 })
      await expect(
        access.loginSession(
          ldapEvent({ method: 'POST', headers: { origin: 'https://audit.example.com.evil.test' } })
            .event,
          'alice',
          'pw',
        ),
      ).rejects.toMatchObject({ statusCode: 403 })
      // X-Forwarded-Host never overrides the raw Host.
      await expect(
        access.requireSession(
          makeEvent({
            host: 'evil.test',
            headers: { cookie: `kdfp_workspace=${cookie}`, 'x-forwarded-host': LDAP_HOST },
          }).event,
        ),
      ).rejects.toMatchObject({ statusCode: 403 })
    })
  })

  it('ignores forwarded headers for local peer decisions', async () => {
    await withEnv(
      {
        WORKSPACE_AUTH_MODE: undefined,
        WORKSPACE_REDIS_URL: undefined,
        WORKSPACE_PASSWORD: undefined,
        NUXT_PUBLIC_HOST: undefined,
        NUXT_TRUST_PROXY: undefined,
      },
      async () => {
        const access = await import(ACCESS_PATH)
        const spoofed = makeEvent({
          headers: { 'x-forwarded-for': '127.0.0.1', 'x-forwarded-host': '127.0.0.1' },
          remoteAddress: '203.0.113.9',
        })
        await expect(access.requireSession(spoofed.event)).rejects.toMatchObject({
          statusCode: 403,
        })
      },
    )
  })

  it('rate-limits normalized accounts (3/15min) and IPs (20/15min) before any bind', async () => {
    let binds = 0
    await withEnv(LDAP_ENV, async () => {
      const { access } = await ldapSetup({
        authenticate: async (login: string) => {
          binds += 1
          return { dn: `cn=${login},dc=x`, account: login.toLowerCase() }
        },
      })
      const attempt = (login: string, ip: string) => {
        const { event } = makeEvent({
          method: 'POST',
          url: '/api/session',
          host: LDAP_HOST,
          headers: { ...LDAP_HEADERS },
          remoteAddress: ip,
        })
        return access.loginSession(event, login, 'pw')
      }
      for (let i = 0; i < 3; i++) await attempt('ratelimited', `10.9.0.${i + 1}`)
      expect(binds).toBe(3)
      // Aliased spellings share one budget: bare, DOMAIN\\user, user@domain.
      await expect(attempt('EXAMPLE\\RateLimited', '10.9.0.9')).rejects.toMatchObject({
        statusCode: 401,
      })
      await expect(attempt('ratelimited@example.com', '10.9.0.10')).rejects.toMatchObject({
        statusCode: 401,
      })
      expect(binds).toBe(3)
    })
    binds = 0
    await withEnv(LDAP_ENV, async () => {
      const { access } = await ldapSetup({
        authenticate: async (login: string) => {
          binds += 1
          return { dn: `cn=${login},dc=x`, account: login.toLowerCase() }
        },
      })
      // Concurrent burst from one IP: IP budget consumed first, at most 20 binds.
      const burst = await Promise.allSettled(
        Array.from({ length: 25 }, (_, i) => {
          const { event } = makeEvent({
            method: 'POST',
            url: '/api/session',
            host: LDAP_HOST,
            headers: { ...LDAP_HEADERS },
            remoteAddress: '10.8.0.1',
          })
          return access.loginSession(event, `burst${i}`, 'pw')
        }),
      )
      expect(burst.filter((r) => r.status === 'fulfilled').length).toBe(20)
      expect(binds).toBe(20)
    })
  })

  it('revokes one user and all sessions via generations', async () => {
    await withEnv(LDAP_ENV, async () => {
      const { access, mem } = await ldapSetup({
        authenticate: async (login: string) => ({
          dn: `cn=${login},dc=x`,
          account: login.toLowerCase(),
        }),
      })
      const loginAs = async (name: string) => {
        const { event, res } = ldapEvent({ method: 'POST' })
        await access.loginSession(event, name, 'pw')
        return sessionCookie(res)
      }
      const alice = await loginAs('alice')
      const bob = await loginAs('bob')
      // Concurrent login for alice stays valid until revocation lands.
      const aliceSecond = await loginAs('alice')
      await access.requireSession(ldapEvent({ cookie: aliceSecond }).event)
      await mem.revokeUser('alice')
      await expect(access.requireSession(ldapEvent({ cookie: alice }).event)).rejects.toMatchObject(
        { statusCode: 401 },
      )
      await expect(
        access.requireSession(ldapEvent({ cookie: aliceSecond }).event),
      ).rejects.toMatchObject({ statusCode: 401 })
      await access.requireSession(ldapEvent({ cookie: bob }).event)
      await mem.revokeAll()
      await expect(access.requireSession(ldapEvent({ cookie: bob }).event)).rejects.toMatchObject({
        statusCode: 401,
      })
    })
  })

  it('rechecks AD access after 5 minutes and denies stale sessions', async () => {
    let member = true
    await withEnv(LDAP_ENV, async () => {
      const { access, mem } = await ldapSetup({
        authenticate: async () => ({ dn: 'cn=alice,dc=x', account: 'alice' }),
        check: async () => member,
      })
      const { event, res } = ldapEvent({ method: 'POST' })
      await access.loginSession(event, 'alice', 'pw')
      const cookie = sessionCookie(res)
      member = false
      const rec = await mem.get(cookie)
      if (rec) await mem.write(cookie, { ...rec, checked: Date.now() - 6 * 60 * 1000 }, 8 * 3600)
      await expect(access.requireSession(ldapEvent({ cookie }).event)).rejects.toMatchObject({
        statusCode: 401,
      })
    })
  })

  it('a successful recheck keeps the absolute TTL deadline instead of extending it', async () => {
    await withEnv(LDAP_ENV, async () => {
      const { access, mem } = await ldapSetup({
        authenticate: async () => ({ dn: 'cn=alice,dc=x', account: 'alice' }),
      })
      const { event, res } = ldapEvent({ method: 'POST' })
      await access.loginSession(event, 'alice', 'pw')
      const cookie = sessionCookie(res)
      const rec = await mem.get(cookie)
      if (!rec) throw new Error('expected session record')
      // One minute of life left, recheck due, membership intact.
      const aged = {
        ...rec,
        created: Date.now() - (8 * 3600 * 1000 - 60 * 1000),
        checked: Date.now() - 6 * 60 * 1000,
      }
      await mem.write(cookie, aged, 8 * 3600)
      await access.requireSession(ldapEvent({ cookie }).event)
      const after = await mem.get(cookie)
      expect(after?.created).toBe(aged.created)
      // Past the absolute deadline the session is dead even with a fresh check.
      await mem.write(
        cookie,
        { ...aged, created: Date.now() - 8 * 3600 * 1000 - 1000, checked: Date.now() },
        3600,
      )
      await expect(access.requireSession(ldapEvent({ cookie }).event)).rejects.toMatchObject({
        statusCode: 401,
      })
    })
  })

  it('logout during an in-flight AD recheck does not resurrect the session', async () => {
    const { promise: entered, resolve: markEntered } = Promise.withResolvers<void>()
    const { promise: gate, resolve: releaseCheck } = Promise.withResolvers<boolean>()
    await withEnv(LDAP_ENV, async () => {
      const { access, mem } = await ldapSetup({
        authenticate: async () => ({ dn: 'cn=alice,dc=x', account: 'alice' }),
        check: () => {
          markEntered()
          return gate
        },
      })
      const { event, res } = ldapEvent({ method: 'POST' })
      await access.loginSession(event, 'alice', 'pw')
      const cookie = sessionCookie(res)
      const rec = await mem.get(cookie)
      if (rec) await mem.write(cookie, { ...rec, checked: Date.now() - 6 * 60 * 1000 }, 8 * 3600)
      const pending = access.requireSession(ldapEvent({ cookie }).event)
      await entered
      await access.logoutSession(ldapEvent({ method: 'DELETE', cookie }).event)
      releaseCheck(true)
      await expect(pending).rejects.toMatchObject({ statusCode: 401 })
      await expect(access.requireSession(ldapEvent({ cookie }).event)).rejects.toMatchObject({
        statusCode: 401,
      })
    })
  })

  it('logout fails 503 without clearing the cookie when the store is down, retry succeeds', async () => {
    await withEnv(LDAP_ENV, async () => {
      mockLdap()
      const savedMode = process.env.WORKSPACE_AUTH_MODE
      const savedUrl = process.env.WORKSPACE_REDIS_URL
      delete process.env.WORKSPACE_AUTH_MODE
      delete process.env.WORKSPACE_REDIS_URL
      const real = await import(STORE_PATH)
      const backing = real.getSessionStore()
      if (savedMode === undefined) delete process.env.WORKSPACE_AUTH_MODE
      else process.env.WORKSPACE_AUTH_MODE = savedMode
      if (savedUrl === undefined) delete process.env.WORKSPACE_REDIS_URL
      else process.env.WORKSPACE_REDIS_URL = savedUrl
      const failing = {
        write: (...args: Parameters<typeof backing.write>) => backing.write(...args),
        get: (...args: Parameters<typeof backing.get>) => backing.get(...args),
        remove: async (..._args: Parameters<typeof backing.remove>) => {
          void _args
          throw new real.StoreUnavailableError('down')
        },
        userGen: (...args: Parameters<typeof backing.userGen>) => backing.userGen(...args),
        globalGen: (...args: Parameters<typeof backing.globalGen>) => backing.globalGen(...args),
        revokeUser: (...args: Parameters<typeof backing.revokeUser>) => backing.revokeUser(...args),
        revokeAll: (...args: Parameters<typeof backing.revokeAll>) => backing.revokeAll(...args),
        hitRateLimit: (...args: Parameters<typeof backing.hitRateLimit>) =>
          backing.hitRateLimit(...args),
      }
      vi.doMock(STORE_PATH, () => ({ ...real, getSessionStore: () => failing }))
      const access = await import(ACCESS_PATH)
      const { event, res } = ldapEvent({ method: 'POST' })
      await access.loginSession(event, 'alice', 'pw')
      const cookie = sessionCookie(res)
      const logout = ldapEvent({ method: 'DELETE', cookie })
      await expect(access.logoutSession(logout.event)).rejects.toMatchObject({ statusCode: 503 })
      // Cookie kept so the retry can target the same session.
      expect(setCookies(logout.res).filter((c) => c.startsWith('kdfp_workspace='))).toEqual([])
      expect(await backing.get(cookie)).not.toBeNull()
      // Store recovers: the same backing serves a fresh module copy, retry revokes.
      vi.resetModules()
      mockLdap()
      const real2 = await import(STORE_PATH)
      vi.doMock(STORE_PATH, () => ({ ...real2, getSessionStore: () => backing }))
      const access2 = await import(ACCESS_PATH)
      await access2.logoutSession(ldapEvent({ method: 'DELETE', cookie }).event)
      await expect(access2.requireSession(ldapEvent({ cookie }).event)).rejects.toMatchObject({
        statusCode: 401,
      })
    })
  })

  it('fails closed when the store is unavailable', async () => {
    mockLdap()
    await withEnv(
      {
        WORKSPACE_AUTH_MODE: 'ldap',
        WORKSPACE_REDIS_URL: 'redis://127.0.0.1:9/1',
        WORKSPACE_ORIGIN: 'https://audit.example.com',
      },
      async () => {
        const access = await import(ACCESS_PATH)
        await expect(
          access.loginSession(ldapEvent({ method: 'POST' }).event, 'alice', 'pw'),
        ).rejects.toMatchObject({ statusCode: 503 })
      },
    )
  })
})

describe('HTTP access boundaries', () => {
  it('serves the LDAP gate using configured origin without the legacy host setting', async () => {
    await withEnv(
      {
        WORKSPACE_AUTH_MODE: 'ldap',
        WORKSPACE_ORIGIN: 'https://audit.example.com',
        NUXT_PUBLIC_HOST: undefined,
      },
      async () => {
        // Load after env/module reset to exercise the actual route boundary.
        const { default: gate } = await import('../../server/api/gate.get')
        expect(await gate(ldapEvent({ headers: {} }).event)).toEqual({ locked: true, mode: 'ldap' })
      },
    )
  })

  it('denies both API data and file download routes before reaching Python without a session', async () => {
    await withEnv(
      {
        WORKSPACE_AUTH_MODE: 'ldap',
        WORKSPACE_ORIGIN: 'https://audit.example.com',
      },
      async () => {
        // No runtime config/backend exists here: authorization must run first.
        const { default: gateway } = await import('../../server/api/[...path]')
        for (const path of ['/api/v1/projects', '/api/v1/documents/secret/content']) {
          const { event } = makeEvent({ url: path, host: LDAP_HOST })
          await expect(gateway(event)).rejects.toMatchObject({ statusCode: 401 })
        }
      },
    )
  })
})
