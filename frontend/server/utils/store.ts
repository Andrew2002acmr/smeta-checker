import IORedis from 'ioredis'

type RedisClient = InstanceType<typeof IORedis>

export interface SessionRecord {
  v: 1
  id: string
  /** 'local' for shared-password mode, normalized account name in LDAP mode. Never a password. */
  account: string
  /** LDAP user DN, empty for local sessions. */
  dn: string
  mode: 'local' | 'ldap'
  /** Generation counters observed at creation; mismatch means revoked. */
  ug: number
  ag: number
  created: number
  checked: number
}

export interface SessionStore {
  write(id: string, rec: SessionRecord, ttlSec: number): Promise<void>
  /** Updates an existing record only; never resurrects a logged-out session. */
  refresh(id: string, rec: SessionRecord, ttlSec: number): Promise<boolean>
  get(id: string): Promise<SessionRecord | null>
  remove(id: string): Promise<void>
  userGen(account: string): Promise<number>
  globalGen(): Promise<number>
  revokeUser(account: string): Promise<void>
  revokeAll(): Promise<void>
  /** Atomically increments the window counter; resolves true when under the limit. */
  hitRateLimit(key: string, windowSec: number, limit: number): Promise<boolean>
}

export class StoreUnavailableError extends Error {
  constructor(message = 'Session store unavailable') {
    super(message)
    this.name = 'StoreUnavailableError'
  }
}

/**
 * Redis key contract (revocation operators use plain redis-cli, no scripts):
 *   INCR kdfp:ugen:<encodeURIComponent(lowercased account)>  — revoke one user
 *   INCR kdfp:agen                                            — revoke all sessions
 * Session records expire by TTL; no deletion scan is needed. Never pass
 * secrets as CLI args; use `docker compose exec redis redis-cli -n 1 …`.
 */
export const REDIS_SESSION_PREFIX = 'kdfp:sess:'
export const REDIS_USER_GEN_PREFIX = 'kdfp:ugen:'
export const REDIS_GLOBAL_GEN = 'kdfp:agen'

export function redisUserGenKey(account: string): string {
  return REDIS_USER_GEN_PREFIX + encodeURIComponent(String(account).toLowerCase())
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return !!value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function isSessionRecord(value: unknown): value is SessionRecord {
  const rec = asRecord(value)
  return (
    !!rec &&
    rec.v === 1 &&
    typeof rec.id === 'string' &&
    typeof rec.account === 'string' &&
    typeof rec.dn === 'string' &&
    (rec.mode === 'local' || rec.mode === 'ldap') &&
    typeof rec.ug === 'number' &&
    typeof rec.ag === 'number' &&
    typeof rec.created === 'number' &&
    typeof rec.checked === 'number'
  )
}

class MemorySessionStore implements SessionStore {
  private sessions = new Map<string, { rec: SessionRecord; expires: number }>()
  private userGens = new Map<string, number>()
  private globalGenCount = 0
  private limits = new Map<string, { count: number; reset: number }>()

  async write(id: string, rec: SessionRecord, ttlSec: number): Promise<void> {
    this.sessions.set(id, { rec, expires: Date.now() + Math.max(0, ttlSec) * 1000 })
  }

  async refresh(id: string, rec: SessionRecord, ttlSec: number): Promise<boolean> {
    const existing = this.sessions.get(id)
    if (!existing || existing.expires <= Date.now()) return false
    this.sessions.set(id, { rec, expires: Date.now() + Math.max(0, ttlSec) * 1000 })
    return true
  }

  async get(id: string): Promise<SessionRecord | null> {
    const entry = this.sessions.get(id)
    if (!entry) return null
    if (entry.expires <= Date.now()) {
      this.sessions.delete(id)
      return null
    }
    return isSessionRecord(entry.rec) && entry.rec.id === id ? entry.rec : null
  }

  async remove(id: string): Promise<void> {
    this.sessions.delete(id)
  }

  async userGen(account: string): Promise<number> {
    return this.userGens.get(account) ?? 0
  }

  async globalGen(): Promise<number> {
    return this.globalGenCount
  }

  async revokeUser(account: string): Promise<void> {
    this.userGens.set(account, (this.userGens.get(account) ?? 0) + 1)
  }

  async revokeAll(): Promise<void> {
    this.globalGenCount += 1
  }

  async hitRateLimit(key: string, windowSec: number, limit: number): Promise<boolean> {
    const now = Date.now()
    const entry = this.limits.get(key)
    if (!entry || entry.reset <= now) {
      this.limits.set(key, { count: 1, reset: now + windowSec * 1000 })
      return 1 <= limit
    }
    entry.count += 1
    return entry.count <= limit
  }
}

const RATE_LIMIT_LUA = `local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return c`

class RedisSessionStore implements SessionStore {
  private client: RedisClient
  private connecting: Promise<void> | null = null

  constructor(url: string) {
    this.client = new IORedis(url, {
      lazyConnect: true,
      enableOfflineQueue: false,
      maxRetriesPerRequest: 1,
      connectTimeout: 5000,
      commandTimeout: 5000,
      retryStrategy: () => null,
    })
    this.client.on('error', () => {})
  }

  /** Single-flight explicit connect: lazyConnect never dials on its own. */
  private ensureConnected(): Promise<void> {
    if (this.client.status === 'ready') return Promise.resolve()
    if (!this.connecting) {
      this.connecting = this.client
        .connect()
        .then(
          () => undefined,
          () => {
            throw new StoreUnavailableError()
          },
        )
        .finally(() => {
          this.connecting = null
        })
    }
    return this.connecting
  }

  private async run<T>(op: (client: RedisClient) => Promise<T>): Promise<T> {
    await this.ensureConnected()
    try {
      return await op(this.client)
    } catch (error) {
      if (error instanceof StoreUnavailableError) throw error
      throw new StoreUnavailableError()
    }
  }

  async write(id: string, rec: SessionRecord, ttlSec: number): Promise<void> {
    await this.run(async (client) => {
      const ttl = Math.max(1, Math.floor(ttlSec))
      await client.set(REDIS_SESSION_PREFIX + id, JSON.stringify(rec), 'EX', ttl)
    })
  }

  async refresh(id: string, rec: SessionRecord, ttlSec: number): Promise<boolean> {
    return this.run(
      async (client) =>
        (await client.set(
          REDIS_SESSION_PREFIX + id,
          JSON.stringify(rec),
          'EX',
          Math.max(1, Math.floor(ttlSec)),
          'XX',
        )) === 'OK',
    )
  }

  async get(id: string): Promise<SessionRecord | null> {
    const raw = await this.run((client) => client.get(REDIS_SESSION_PREFIX + id))
    if (!raw) return null
    try {
      const parsed: unknown = JSON.parse(raw)
      return isSessionRecord(parsed) && parsed.id === id ? parsed : null
    } catch {
      return null
    }
  }

  async remove(id: string): Promise<void> {
    await this.run((client) => client.del(REDIS_SESSION_PREFIX + id).then(() => undefined))
  }

  async userGen(account: string): Promise<number> {
    const raw = await this.run((client) => client.get(redisUserGenKey(account)))
    const value = Number(raw ?? 0)
    return Number.isFinite(value) ? value : 0
  }

  async globalGen(): Promise<number> {
    const raw = await this.run((client) => client.get(REDIS_GLOBAL_GEN))
    const value = Number(raw ?? 0)
    return Number.isFinite(value) ? value : 0
  }

  /** Generation bump only; records expire by TTL, no deletion scan. */
  async revokeUser(account: string): Promise<void> {
    await this.run((client) => client.incr(redisUserGenKey(account)).then(() => undefined))
  }

  /** Generation bump only; records expire by TTL, no deletion scan. */
  async revokeAll(): Promise<void> {
    await this.run((client) => client.incr(REDIS_GLOBAL_GEN).then(() => undefined))
  }

  async hitRateLimit(key: string, windowSec: number, limit: number): Promise<boolean> {
    const count = await this.run(
      (client) =>
        client.eval(
          RATE_LIMIT_LUA,
          1,
          key,
          String(Math.max(1, Math.floor(windowSec))),
        ) as Promise<number>,
    )
    return Number(count) <= limit
  }
}

let cached: SessionStore | null = null
let cachedKey = ''

/**
 * Redis in LDAP mode and whenever WORKSPACE_REDIS_URL is set; memory fallback
 * for Redis-less local stands only. LDAP without Redis fails closed with
 * StoreUnavailableError. The cache key includes mode+URL so tests (or a
 * reconfigured process) never reuse a memory store for LDAP.
 */
export function getSessionStore(): SessionStore {
  const url = (process.env.WORKSPACE_REDIS_URL || '').trim()
  const mode = (process.env.WORKSPACE_AUTH_MODE || '').trim().toLowerCase()
  const key = `${mode}\n${url}`
  if (cached && cachedKey === key) return cached
  let next: SessionStore
  if (url) {
    try {
      const parsed = new URL(url)
      if (!['redis:', 'rediss:'].includes(parsed.protocol)) throw new StoreUnavailableError()
      next = new RedisSessionStore(url)
    } catch {
      throw new StoreUnavailableError()
    }
  } else if (mode === 'ldap') throw new StoreUnavailableError()
  else next = new MemorySessionStore()
  cached = next
  cachedKey = key
  return cached
}
