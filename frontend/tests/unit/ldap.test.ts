import { beforeEach, describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({
  instances: [] as FakeClient[],
  searchImpl: null as
    | null
    | ((baseDN: string, options: SearchOpts) => Promise<{ searchEntries: EntryLike[] }>),
  bindImpl: null as null | ((dn: string, password?: string) => Promise<void>),
  startTLSImpl: null as null | ((options: Record<string, unknown>) => Promise<void>),
  startTLSCalls: [] as Record<string, unknown>[],
  allowPlaintext: false,
}))

interface FakeClientOptions {
  url?: unknown
  timeout?: unknown
  connectTimeout?: unknown
  autoRebind?: unknown
  createConnection?: (...args: unknown[]) => unknown
  createSecureConnection?: (...args: unknown[]) => unknown
}

interface SearchOpts {
  scope?: string
  filter?: string
  attributes?: string[]
  sizeLimit?: number
  timeLimit?: number
  derefAliases?: string
}

type EntryLike = Record<string, unknown> & { dn: string }

class FakeClient {
  static instances = state.instances
  options: FakeClientOptions
  encrypted = false
  searchCalls: { baseDN: string; options: SearchOpts }[] = []
  bindCalls: { dn: string; password?: string }[] = []
  unbindCalls = 0
  constructor(options: FakeClientOptions) {
    this.options = options
    state.instances.push(this)
  }
  async startTLS(options: Record<string, unknown> = {}): Promise<void> {
    state.startTLSCalls.push(options)
    if (state.startTLSImpl) await state.startTLSImpl(options)
    this.encrypted = true
  }
  async bind(dn: string, password?: string): Promise<void> {
    if (String(this.options.url).startsWith('ldap://') && !this.encrypted && !state.allowPlaintext)
      throw new Error('Credentials sent before STARTTLS')
    this.bindCalls.push({ dn, password })
    if (state.bindImpl) await state.bindImpl(dn, password)
  }
  async search(baseDN: string, options: SearchOpts = {}): Promise<{ searchEntries: EntryLike[] }> {
    this.searchCalls.push({ baseDN, options })
    if (state.searchImpl) {
      const result = await state.searchImpl(baseDN, options)
      // Model AD DN equality once: every mock honors negated exclusions,
      // so no test can echo an excluded identity back into the result.
      return { searchEntries: withoutExcluded(result.searchEntries, options.filter || '') }
    }
    return { searchEntries: [] }
  }
  async unbind(): Promise<void> {
    this.unbindCalls += 1
  }
}

vi.doMock('ldapts', () => {
  class InvalidCredentialsError extends Error {
    code = 49
    constructor(message = 'Invalid credentials') {
      super(`${message} Code: 0x31`)
      this.name = 'InvalidCredentialsError'
    }
  }
  return { Client: FakeClient, InvalidCredentialsError }
})

// Deliberately load after doMock: these tests exercise the LDAP module boundary.
const { InvalidCredentialsError } = await import('ldapts')
const { AuthDeniedError, DirectoryUnavailableError, authenticateLdap, checkLdapAccess } =
  await import('../../server/utils/ldap')

const USER_DN = 'CN=ivan,OU=Users,DC=example,DC=com'
const GROUP_DN = 'CN=app-users,OU=Groups,DC=example,DC=com'
const SVC_DN = 'CN=svc,OU=Svc,DC=example,DC=com'
const SVC_ACCOUNT = 'svc'

function enabledEntry(overrides: Record<string, unknown> = {}): EntryLike {
  return {
    dn: USER_DN,
    sAMAccountName: 'ivan',
    userAccountControl: '512',
    accountExpires: '0',
    'msDS-User-Account-Control-Computed': '0',
    ...overrides,
  }
}

function unescapeFilterValue(value: string): string {
  return value
    .replace(/\\28/gi, '(')
    .replace(/\\29/gi, ')')
    .replace(/\\2a/gi, '*')
    .replace(/\\5c/gi, '\\')
    .replace(/\\00/gi, '\0')
}

/** DNs the filter explicitly excludes via (!(distinguishedName=...)). */
function filterExcludedNames(filter: string): string[] {
  const out: string[] = []
  const re = /!\(distinguishedName=([^)]+)\)\)/g
  let match: RegExpExecArray | null
  while ((match = re.exec(filter)) !== null) {
    const value = match[1]
    if (value !== undefined) out.push(unescapeFilterValue(value).toLowerCase())
  }
  return out
}
/** Model AD DN equality: excluded entries never come back once filtered. */
function withoutExcluded(entries: EntryLike[], filter: string): EntryLike[] {
  const excluded = filterExcludedNames(filter)
  if (excluded.length === 0) return entries
  return entries.filter((entry) => !excluded.includes(entry.dn.toLowerCase()))
}

/** Positive distinguishedName assertions in a filter (recheck target + exclusion). */
function filterTargetNames(filter: string): string[] {
  const out: string[] = []
  const re = /\(distinguishedName=([^)]+)\)/g
  let match: RegExpExecArray | null
  while ((match = re.exec(filter)) !== null) {
    const value = match[1]
    if (value !== undefined) out.push(unescapeFilterValue(value).toLowerCase())
  }
  return out
}

const BASE_DN = 'DC=example,DC=com'

function svcStatusEntry(dn = SVC_DN): EntryLike {
  return {
    dn,
    sAMAccountName: SVC_ACCOUNT,
    userAccountControl: '512',
    accountExpires: '0',
    'msDS-User-Account-Control-Computed': '0',
  }
}

/**
 * All-mode directory: the service base lookup resolves the canonical DN,
 * logins route by account name, rechecks route by distinguishedName target
 * (unknown/out-of-base targets come back empty, as AD would).
 */
function setAllDirectory(userAttrs: Record<string, unknown> = {}): void {
  state.searchImpl = async (_baseDN, options) => {
    const filter = options.filter || ''
    if (options.scope === 'base' && filter === '(objectClass=user)')
      return { searchEntries: [svcStatusEntry()] }
    const account = /sAMAccountName=([^)]+)\)/.exec(filter)?.[1]
    if (account !== undefined)
      return {
        searchEntries: account === SVC_ACCOUNT ? [svcStatusEntry()] : [enabledEntry(userAttrs)],
      }
    if (filter.includes('(distinguishedName=')) {
      const target = filterTargetNames(filter)[0]
      if (target === USER_DN.toLowerCase()) return { searchEntries: [enabledEntry(userAttrs)] }
      if (target === SVC_DN.toLowerCase()) return { searchEntries: [svcStatusEntry()] }
      return { searchEntries: [] }
    }
    if (filter.includes('memberOf')) return { searchEntries: [enabledEntry(userAttrs)] }
    return { searchEntries: [] }
  }
  state.bindImpl = async () => {}
  state.startTLSImpl = async () => {}
}

function setEnv(extra: Record<string, string | undefined> = {}): void {
  process.env.LDAP_URL = 'ldap://ad.example.com:389'
  process.env.LDAP_DOMAIN = 'example.com'
  process.env.LDAP_BASE_DN = 'DC=example,DC=com'
  process.env.LDAP_BIND_DN = SVC_DN
  process.env.LDAP_BIND_PASSWORD = 'svc-secret'
  process.env.LDAP_GROUP_DN = GROUP_DN
  delete process.env.LDAP_CA_FILE
  delete process.env.LDAP_TLS_MODE
  delete process.env.LDAP_ACCESS_MODE
  for (const [key, value] of Object.entries(extra)) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
}

/** Default happy-path directory: one enabled user, good password, member. */
function setHappyPath(): void {
  state.searchImpl = async (_baseDN, options) => {
    const filter = options.filter || ''
    if (filter.includes('sAMAccountName')) return { searchEntries: [enabledEntry()] }
    if (filter.includes('memberOf')) return { searchEntries: [enabledEntry()] }
    return { searchEntries: [enabledEntry()] }
  }
  state.bindImpl = async () => {}
  state.startTLSImpl = async () => {}
}

beforeEach(() => {
  state.instances.length = 0
  state.startTLSCalls.length = 0
  state.searchImpl = null
  state.bindImpl = null
  state.startTLSImpl = null
  state.allowPlaintext = false
  setEnv()
})

describe('authenticateLdap', () => {
  it('authenticates bare, DOMAIN\\user and user@domain logins for the configured domain', async () => {
    setHappyPath()
    for (const login of ['ivan', 'EXAMPLE.COM\\ivan', 'ivan@example.com']) {
      state.instances.length = 0
      await expect(authenticateLdap(login, 'secret')).resolves.toEqual({
        dn: USER_DN,
        account: 'ivan',
      })
    }
  })

  it('denies empty login/password before opening any connection', async () => {
    setHappyPath()
    await expect(authenticateLdap('', 'secret')).rejects.toBeInstanceOf(AuthDeniedError)
    await expect(authenticateLdap('ivan', '')).rejects.toBeInstanceOf(AuthDeniedError)
    await expect(authenticateLdap('   ', 'secret')).rejects.toBeInstanceOf(AuthDeniedError)
    expect(state.instances.length).toBe(0)
  })

  it('denies foreign domains without contacting the directory', async () => {
    setHappyPath()
    await expect(authenticateLdap('OTHER\\ivan', 'secret')).rejects.toBeInstanceOf(AuthDeniedError)
    await expect(authenticateLdap('ivan@other.com', 'secret')).rejects.toBeInstanceOf(
      AuthDeniedError,
    )
    expect(state.instances.length).toBe(0)
  })

  it('denies LDAP metacharacters before any connection (injection fail-closed)', async () => {
    setHappyPath()
    await expect(authenticateLdap('*)(uid=*', 'secret')).rejects.toBeInstanceOf(AuthDeniedError)
    await expect(authenticateLdap('ivan)(objectClass=*', 'secret')).rejects.toBeInstanceOf(
      AuthDeniedError,
    )
    expect(state.instances.length).toBe(0)
  })

  it('denies on ambiguous search results (zero or many entries)', async () => {
    setHappyPath()
    state.searchImpl = async (_baseDN, options) =>
      options.filter?.includes('sAMAccountName')
        ? { searchEntries: [enabledEntry(), enabledEntry({ dn: 'CN=ivan2' })] }
        : { searchEntries: [enabledEntry()] }
    await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(AuthDeniedError)

    state.searchImpl = async (_baseDN, options) =>
      options.filter?.includes('sAMAccountName')
        ? { searchEntries: [] }
        : { searchEntries: [enabledEntry()] }
    await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(AuthDeniedError)
  })

  it('denies bad passwords without retrying the bind', async () => {
    setHappyPath()
    state.bindImpl = async (dn) => {
      if (dn === USER_DN) throw new InvalidCredentialsError()
    }
    await expect(authenticateLdap('ivan', 'wrong')).rejects.toBeInstanceOf(AuthDeniedError)
    const userBinds = state.instances.flatMap((c) => c.bindCalls.filter((b) => b.dn === USER_DN))
    expect(userBinds.length).toBe(1)
  })

  it('denies disabled, locked and expired accounts', async () => {
    setHappyPath()
    for (const attrs of [
      { userAccountControl: '514' },
      { userAccountControl: '528' },
      { 'msDS-User-Account-Control-Computed': '16' },
      { 'msDS-User-Account-Control-Computed': undefined },
      { accountExpires: '132000000000000000' },
    ]) {
      state.searchImpl = async (_baseDN, options) =>
        options.filter?.includes('sAMAccountName')
          ? { searchEntries: [enabledEntry(attrs)] }
          : { searchEntries: [enabledEntry(attrs)] }
      await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(AuthDeniedError)
    }
  })

  it('denies when the user is not a (nested) group member', async () => {
    setHappyPath()
    state.searchImpl = async (_baseDN, options) => {
      const filter = options.filter || ''
      if (filter.includes('sAMAccountName')) return { searchEntries: [enabledEntry()] }
      if (filter.includes('memberOf')) return { searchEntries: [] }
      return { searchEntries: [enabledEntry()] }
    }
    await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(AuthDeniedError)
  })

  it('uses the nested group chain matching rule and escapes the group DN', async () => {
    setEnv({ LDAP_GROUP_DN: 'CN=a)b,OU=Groups,DC=example,DC=com' })
    setHappyPath()
    await authenticateLdap('ivan', 'secret')
    const groupSearch = state.instances
      .flatMap((c) => c.searchCalls)
      .find((s) => s.options.filter?.includes('memberOf'))
    expect(groupSearch?.options.filter).toContain('1.2.840.113556.1.4.1941')
    expect(groupSearch?.options.filter).toContain('CN=a\\29b')
  })

  it('requires STARTTLS with TLS 1.2+ on ldap:// before the first bind', async () => {
    setHappyPath()
    await authenticateLdap('ivan', 'secret')
    expect(state.startTLSCalls).toHaveLength(2)
    for (const options of state.startTLSCalls) {
      expect(options.minVersion).toBe('TLSv1.2')
      expect(options.rejectUnauthorized).toBe(true)
    }
  })

  it('maps network/timeout/STARTTLS failures to unavailable and closes connections', async () => {
    setHappyPath()
    state.searchImpl = async () => {
      throw new Error('socket hang up')
    }
    await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(
      DirectoryUnavailableError,
    )
    expect(state.instances.length).toBeGreaterThan(0)
    for (const client of state.instances) expect(client.unbindCalls).toBe(1)

    state.instances.length = 0
    setHappyPath()
    state.startTLSImpl = async () => {
      throw new Error('certificate has expired')
    }
    await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(
      DirectoryUnavailableError,
    )
    for (const client of state.instances) expect(client.unbindCalls).toBe(1)
  })

  it('treats invalid configuration as unavailable, never a downgrade', async () => {
    setHappyPath()
    setEnv({ LDAP_URL: 'ldap://ad.example.com' })
    await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(
      DirectoryUnavailableError,
    )
    setEnv({ LDAP_URL: 'http://ad.example.com:389' })
    await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(
      DirectoryUnavailableError,
    )
    setEnv({ LDAP_BIND_PASSWORD: undefined })
    await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(
      DirectoryUnavailableError,
    )
    expect(state.instances.length).toBe(0)
  })

  it('never exposes directory internals in denied errors', async () => {
    setHappyPath()
    state.searchImpl = async () => {
      throw new Error('connect ECONNREFUSED 10.0.0.5:389')
    }
    const error: unknown = await authenticateLdap('ivan', 'secret').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(DirectoryUnavailableError)
    const message = error instanceof Error ? error.message : String(error)
    expect(message).not.toContain('10.0.0.5')
    expect(message).not.toContain('secret')
  })
})

describe('checkLdapAccess', () => {
  it('returns true for enabled members, false for non-members', async () => {
    setHappyPath()
    await expect(checkLdapAccess(USER_DN)).resolves.toBe(true)
    state.searchImpl = async (_baseDN, options) =>
      options.filter?.includes('memberOf')
        ? { searchEntries: [] }
        : { searchEntries: [enabledEntry()] }
    await expect(checkLdapAccess(USER_DN)).resolves.toBe(false)
  })

  it('returns false for disabled accounts without throwing', async () => {
    state.searchImpl = async () => ({
      searchEntries: [enabledEntry({ userAccountControl: '514' })],
    })
    state.bindImpl = async () => {}
    state.startTLSImpl = async () => {}
    await expect(checkLdapAccess(USER_DN)).resolves.toBe(false)
  })

  it('rejects empty DNs and surfaces directory failures as unavailable', async () => {
    setHappyPath()
    await expect(checkLdapAccess('')).rejects.toBeInstanceOf(AuthDeniedError)
    state.searchImpl = async () => {
      throw new Error('timeout')
    }
    await expect(checkLdapAccess(USER_DN)).rejects.toBeInstanceOf(DirectoryUnavailableError)
  })
})

describe('LDAP_ACCESS_MODE=all authentication', () => {
  it('authenticates employees outside the group with no group configured', async () => {
    setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_GROUP_DN: undefined })
    setAllDirectory()
    await expect(authenticateLdap('ivan', 'secret')).resolves.toEqual({
      dn: USER_DN,
      account: 'ivan',
    })
    const memberSearches = state.instances
      .flatMap((c) => c.searchCalls)
      .filter((s) => s.options.filter?.includes('memberOf'))
    expect(memberSearches).toHaveLength(0)
  })

  it('still denies disabled, locked and expired accounts in all mode', async () => {
    setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_GROUP_DN: undefined })
    for (const attrs of [
      { userAccountControl: '514' },
      { userAccountControl: '528' },
      { 'msDS-User-Account-Control-Computed': '16' },
      { accountExpires: '132000000000000000' },
    ]) {
      setAllDirectory(attrs)
      await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(AuthDeniedError)
    }
  })

  it('denies the service identity and equivalent DN spellings before the user bind', async () => {
    setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_GROUP_DN: undefined })
    // A case-variant spelling names the same AD object; the exclusion still applies.
    for (const serviceDN of [SVC_DN, 'cn=SVC,ou=svc,dc=example,dc=com']) {
      state.instances.length = 0
      state.searchImpl = async (_baseDN, options) => {
        if (options.scope === 'base' && (options.filter || '') === '(objectClass=user)')
          return { searchEntries: [svcStatusEntry(serviceDN)] }
        return { searchEntries: [svcStatusEntry(serviceDN)] }
      }
      state.bindImpl = async () => {}
      state.startTLSImpl = async () => {}
      await expect(authenticateLdap(SVC_ACCOUNT, 'svc-secret')).rejects.toBeInstanceOf(
        AuthDeniedError,
      )
      // Only the service connection exists: no user bind was ever attempted.
      expect(state.instances).toHaveLength(1)
      expect(state.instances.flatMap((c) => c.bindCalls)).toEqual([
        { dn: SVC_DN, password: 'svc-secret' },
      ])
    }
  })

  it('resolves the exact service DN with a base search and excludes it', async () => {
    setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_GROUP_DN: undefined })
    setAllDirectory()
    await authenticateLdap('ivan', 'secret')
    const serviceLookup = state.instances
      .flatMap((c) => c.searchCalls)
      .find((s) => s.options.scope === 'base' && s.options.filter === '(objectClass=user)')
    expect(serviceLookup?.baseDN).toBe(SVC_DN)
    expect(serviceLookup?.options.attributes).toEqual(['1.1'])
    expect(serviceLookup?.options.sizeLimit).toBe(1)
    const loginSearch = state.instances
      .flatMap((c) => c.searchCalls)
      .find((s) => s.options.filter?.includes('sAMAccountName'))
    expect(loginSearch?.baseDN).toBe(BASE_DN)
    expect(loginSearch?.options.scope).toBe('sub')
    expect(loginSearch?.options.filter).toBe(
      `(&(objectClass=user)(sAMAccountName=ivan)(!(distinguishedName=${SVC_DN})))`,
    )
  })

  it('escapes metacharacters in the resolved service DN', async () => {
    const specialDN = 'CN=sv)c,OU=Svc,DC=example,DC=com'
    setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_BIND_DN: specialDN, LDAP_GROUP_DN: undefined })
    state.searchImpl = async (_baseDN, options) => {
      if (options.scope === 'base' && (options.filter || '') === '(objectClass=user)')
        return { searchEntries: [svcStatusEntry(specialDN)] }
      if ((options.filter || '').includes('sAMAccountName'))
        return { searchEntries: [enabledEntry()] }
      return { searchEntries: [] }
    }
    state.bindImpl = async () => {}
    state.startTLSImpl = async () => {}
    await authenticateLdap('ivan', 'secret')
    const loginSearch = state.instances
      .flatMap((c) => c.searchCalls)
      .find((s) => s.options.filter?.includes('sAMAccountName'))
    expect(loginSearch?.options.filter).toContain(
      '(!(distinguishedName=CN=sv\\29c,OU=Svc,DC=example,DC=com))',
    )
  })

  it('defaults empty modes to group and fails closed on unknown modes', async () => {
    for (const mode of [undefined, '', '   ']) {
      state.instances.length = 0
      setEnv({ LDAP_ACCESS_MODE: mode })
      setHappyPath()
      await expect(authenticateLdap('ivan', 'secret')).resolves.toEqual({
        dn: USER_DN,
        account: 'ivan',
      })
    }
    for (const mode of ['everyone', 'ALL-USERS', 'none']) {
      state.instances.length = 0
      state.startTLSCalls.length = 0
      setEnv({ LDAP_ACCESS_MODE: mode })
      setHappyPath()
      await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(
        DirectoryUnavailableError,
      )
      expect(state.instances).toHaveLength(0)
    }
  })

  it('requires a full bind DN in all mode and rejects UPNs before connecting', async () => {
    for (const bindDN of ['svc@example.com', 'svc', '']) {
      state.instances.length = 0
      setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_BIND_DN: bindDN, LDAP_GROUP_DN: undefined })
      setAllDirectory()
      await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(
        DirectoryUnavailableError,
      )
      expect(state.instances).toHaveLength(0)
    }
  })

  it('fails closed when the service lookup is missing, empty or ambiguous', async () => {
    const cases: EntryLike[][] = [
      [],
      [{ ...svcStatusEntry(), dn: '' }],
      [svcStatusEntry(), svcStatusEntry()],
    ]
    for (const entries of cases) {
      state.instances.length = 0
      setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_GROUP_DN: undefined })
      state.searchImpl = async (_baseDN, options) => {
        if (options.scope === 'base' && (options.filter || '') === '(objectClass=user)')
          return { searchEntries: entries }
        return { searchEntries: [enabledEntry()] }
      }
      state.bindImpl = async () => {}
      state.startTLSImpl = async () => {}
      await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(
        DirectoryUnavailableError,
      )
    }
  })
})

describe('LDAP_ACCESS_MODE=all recheck', () => {
  it('authorizes enabled accounts and denies disabled, locked and expired ones', async () => {
    setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_GROUP_DN: undefined })
    setAllDirectory()
    await expect(checkLdapAccess(USER_DN)).resolves.toBe(true)
    for (const attrs of [
      { userAccountControl: '514' },
      { userAccountControl: '528' },
      { 'msDS-User-Account-Control-Computed': '16' },
      { accountExpires: '132000000000000000' },
    ]) {
      setAllDirectory(attrs)
      await expect(checkLdapAccess(USER_DN)).resolves.toBe(false)
    }
  })

  it('treats zero and maximum FILETIME expiry as never-expires', async () => {
    setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_GROUP_DN: undefined })
    for (const accountExpires of ['0', '9223372036854775807']) {
      setAllDirectory({ accountExpires })
      await expect(checkLdapAccess(USER_DN)).resolves.toBe(true)
    }
  })

  it('denies the service identity itself', async () => {
    setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_GROUP_DN: undefined })
    setAllDirectory()
    await expect(checkLdapAccess(SVC_DN)).resolves.toBe(false)
  })

  it('returns false for unknown and out-of-base targets without consulting groups', async () => {
    setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_GROUP_DN: undefined })
    setAllDirectory()
    await expect(checkLdapAccess('CN=ghost,OU=Users,DC=example,DC=com')).resolves.toBe(false)
    await expect(checkLdapAccess('CN=ivan,OU=Users,DC=other,DC=com')).resolves.toBe(false)
    const rechecks = state.instances
      .flatMap((c) => c.searchCalls)
      .filter((s) => s.baseDN === BASE_DN)
    expect(rechecks.length).toBeGreaterThan(0)
    for (const search of rechecks) {
      expect(search.options.scope).toBe('sub')
      expect(search.options.attributes).toEqual([
        'userAccountControl',
        'msDS-User-Account-Control-Computed',
        'accountExpires',
      ])
    }
    const memberSearches = state.instances
      .flatMap((c) => c.searchCalls)
      .filter((s) => s.options.filter?.includes('memberOf'))
    expect(memberSearches).toHaveLength(0)
  })

  it('scopes the recheck to the base DN with target and exclusion filters', async () => {
    setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_GROUP_DN: undefined })
    setAllDirectory()
    await expect(checkLdapAccess(USER_DN)).resolves.toBe(true)
    const recheck = state.instances
      .flatMap((c) => c.searchCalls)
      .find((s) => s.options.filter?.includes('(distinguishedName='))
    expect(recheck?.baseDN).toBe(BASE_DN)
    expect(recheck?.options.scope).toBe('sub')
    expect(recheck?.options.filter).toBe(
      `(&(objectClass=user)(distinguishedName=${USER_DN})(!(distinguishedName=${SVC_DN})))`,
    )
  })

  it('enforces group membership again after switching back to group policy', async () => {
    setEnv({ LDAP_ACCESS_MODE: 'all', LDAP_GROUP_DN: undefined })
    setAllDirectory()
    await expect(checkLdapAccess(USER_DN)).resolves.toBe(true)
    setEnv()
    state.searchImpl = async (_baseDN, options) =>
      options.filter?.includes('memberOf')
        ? { searchEntries: [] }
        : { searchEntries: [enabledEntry()] }
    state.bindImpl = async () => {}
    state.startTLSImpl = async () => {}
    await expect(checkLdapAccess(USER_DN)).resolves.toBe(false)
  })
})

describe('LDAP_TLS_MODE transport selection', () => {
  it('authenticates over explicit none with separate service/user binds and no STARTTLS', async () => {
    setEnv({ LDAP_TLS_MODE: 'none' })
    state.allowPlaintext = true
    setHappyPath()
    await expect(authenticateLdap('ivan', 'secret')).resolves.toEqual({
      dn: USER_DN,
      account: 'ivan',
    })
    expect(state.startTLSCalls).toHaveLength(0)
    expect(state.instances).toHaveLength(2)
    const [service, user] = state.instances
    expect(service.bindCalls).toEqual([
      { dn: 'CN=svc,OU=Svc,DC=example,DC=com', password: 'svc-secret' },
    ])
    expect(user.bindCalls).toEqual([{ dn: USER_DN, password: 'secret' }])
    expect(service.searchCalls.some((s) => s.options.filter?.includes('memberOf'))).toBe(true)
  })

  it('still enforces group membership and account status with explicit none', async () => {
    setEnv({ LDAP_TLS_MODE: 'none' })
    state.allowPlaintext = true
    setHappyPath()
    state.searchImpl = async (_baseDN, options) => {
      const filter = options.filter || ''
      if (filter.includes('sAMAccountName')) return { searchEntries: [enabledEntry()] }
      if (filter.includes('memberOf')) return { searchEntries: [] }
      return { searchEntries: [enabledEntry()] }
    }
    await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(AuthDeniedError)

    state.instances.length = 0
    state.searchImpl = async () => ({
      searchEntries: [enabledEntry({ userAccountControl: '514' })],
    })
    await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(AuthDeniedError)
  })

  it('rechecks access over explicit none without STARTTLS', async () => {
    setEnv({ LDAP_TLS_MODE: 'none' })
    state.allowPlaintext = true
    setHappyPath()
    await expect(checkLdapAccess(USER_DN)).resolves.toBe(true)
    expect(state.startTLSCalls).toHaveLength(0)
    expect(state.instances).toHaveLength(1)
  })

  it('never invokes STARTTLS in none even when the TLS layer would fail', async () => {
    setEnv({ LDAP_TLS_MODE: 'none' })
    state.allowPlaintext = true
    setHappyPath()
    state.startTLSImpl = async () => {
      throw new Error('certificate has expired')
    }
    await expect(authenticateLdap('ivan', 'secret')).resolves.toEqual({
      dn: USER_DN,
      account: 'ivan',
    })
    expect(state.startTLSCalls).toHaveLength(0)
  })

  it('fails closed on unknown modes and scheme mismatches before opening sockets', async () => {
    const cases: Record<string, string | undefined>[] = [
      { LDAP_URL: 'ldap://ad.example.com:389', LDAP_TLS_MODE: 'off' },
      { LDAP_URL: 'ldap://ad.example.com:389', LDAP_TLS_MODE: 'ldaps' },
      { LDAP_URL: 'ldap://ad.example.com:389', LDAP_TLS_MODE: 'plain' },
      { LDAP_URL: 'ldaps://ad.example.com:636', LDAP_TLS_MODE: 'none' },
      { LDAP_URL: 'ldaps://ad.example.com:636', LDAP_TLS_MODE: 'starttls' },
      { LDAP_URL: 'ldaps://ad.example.com:636', LDAP_TLS_MODE: 'off' },
    ]
    for (const extra of cases) {
      state.instances.length = 0
      state.startTLSCalls.length = 0
      setEnv(extra)
      setHappyPath()
      await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(
        DirectoryUnavailableError,
      )
      expect(state.instances.length).toBe(0)
      expect(state.startTLSCalls).toHaveLength(0)
    }
  })

  it('keeps ldaps secure by default and in exact mode without STARTTLS', async () => {
    for (const mode of [undefined, 'ldaps']) {
      state.instances.length = 0
      state.startTLSCalls.length = 0
      setEnv({ LDAP_URL: 'ldaps://ad.example.com:636', LDAP_TLS_MODE: mode })
      setHappyPath()
      await expect(authenticateLdap('ivan', 'secret')).resolves.toEqual({
        dn: USER_DN,
        account: 'ivan',
      })
      expect(state.startTLSCalls).toHaveLength(0)
    }
  })

  it('keeps ldap encrypted by default, in exact starttls mode, and for rechecks', async () => {
    for (const mode of [undefined, 'starttls']) {
      state.instances.length = 0
      state.startTLSCalls.length = 0
      setEnv({ LDAP_URL: 'ldap://ad.example.com:389', LDAP_TLS_MODE: mode })
      setHappyPath()
      await authenticateLdap('ivan', 'secret')
      expect(state.startTLSCalls).toHaveLength(2)
    }
    state.instances.length = 0
    state.startTLSCalls.length = 0
    setEnv()
    setHappyPath()
    await expect(checkLdapAccess(USER_DN)).resolves.toBe(true)
    expect(state.startTLSCalls).toHaveLength(1)
  })

  it('never falls back to plaintext when STARTTLS fails', async () => {
    setHappyPath()
    state.startTLSImpl = async () => {
      throw new Error('certificate has expired')
    }
    await expect(authenticateLdap('ivan', 'secret')).rejects.toBeInstanceOf(
      DirectoryUnavailableError,
    )
    expect(state.instances).toHaveLength(1)
    expect(state.startTLSCalls).toHaveLength(1)
    expect(state.instances.flatMap((c) => c.bindCalls)).toHaveLength(0)
    for (const client of state.instances) expect(client.unbindCalls).toBe(1)
  })
})
