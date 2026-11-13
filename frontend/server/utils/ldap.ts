/**
 * LDAP (Active Directory) authentication.
 *
 * Transport policy:
 * - Empty login/password is denied before any socket is opened.
 * - By default ldap:// upgrades via STARTTLS and ldaps:// uses direct TLS.
 *   TLS 1.2+, certificate trust and hostname checks remain mandatory.
 * - LDAP_TLS_MODE=none explicitly opts into unencrypted ldap:// for a
 *   trusted internal network. TLS failures never trigger this mode.
 * - Each client instance is pinned to a single TCP connection (+ one TLS
 *   upgrade). ldapts would otherwise transparently reconnect on a dropped
 *   socket and could send credentials over a fresh plaintext connection.
 * - No referral following, no retry of failed binds, finite timeouts.
 * - Config problems yield DirectoryUnavailableError, never a downgrade to a
 *   weaker check. Nothing here logs passwords or directory errors.
 */
import { readFileSync } from 'node:fs'
import * as net from 'node:net'
import * as tls from 'node:tls'
import { Client, InvalidCredentialsError } from 'ldapts'
import type { ClientOptions, Entry } from 'ldapts'

export class AuthDeniedError extends Error {
  readonly kind = 'auth_denied' as const
  constructor(message = 'Неверный логин или пароль') {
    super(message)
    this.name = 'AuthDeniedError'
    Object.setPrototypeOf(this, AuthDeniedError.prototype)
  }
}

export class DirectoryUnavailableError extends Error {
  readonly kind = 'directory_unavailable' as const
  constructor(message = 'Служба каталогов временно недоступна') {
    super(message)
    this.name = 'DirectoryUnavailableError'
    Object.setPrototypeOf(this, DirectoryUnavailableError.prototype)
  }
}

const CONNECT_TIMEOUT_MS = 5000
const OPERATION_TIMEOUT_MS = 10000
const SEARCH_TIME_LIMIT_S = 5
const USER_SEARCH_SIZE_LIMIT = 2
const NESTED_GROUP_OID = '1.2.840.113556.1.4.1941'
const NEVER_EXPIRES_FILETIME = 9223372036854775807n
const FILETIME_TO_MS_OFFSET = 11644473600000n
// Pre-Windows-2000 logon names: up to 20 chars, no shell/LDAP metacharacters.
const ACCOUNT_RE = /^[A-Za-z0-9._-]{1,20}$/
const DOMAIN_RE = /^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$/
// userAccountControl bits (AD): disabled, locked out.
const UF_ACCOUNTDISABLE = 0x2
const UF_LOCKOUT = 0x10

interface LdapConfig {
  ldapUrl: string
  host: string
  transport: 'ldaps' | 'starttls' | 'none'
  domain: string
  baseDN: string
  bindDN: string
  bindPassword: string
  groupDN: string
  accessMode: 'group' | 'all'
  ca?: Buffer
}

function loadConfig(): LdapConfig {
  const urlRaw = (process.env.LDAP_URL || '').trim()
  const domain = (process.env.LDAP_DOMAIN || '').trim()
  const baseDN = (process.env.LDAP_BASE_DN || '').trim()
  const bindDN = (process.env.LDAP_BIND_DN || '').trim()
  const bindPassword = process.env.LDAP_BIND_PASSWORD || ''
  const groupDN = (process.env.LDAP_GROUP_DN || '').trim()
  const caFile = (process.env.LDAP_CA_FILE || '').trim()
  const accessMode = (process.env.LDAP_ACCESS_MODE || '').trim().toLowerCase() || 'group'
  if (accessMode !== 'group' && accessMode !== 'all') throw new DirectoryUnavailableError()

  let url: URL
  try {
    url = new URL(urlRaw)
  } catch {
    throw new DirectoryUnavailableError()
  }
  const encrypted = url.protocol.toLowerCase() === 'ldaps:'
  if (!encrypted && url.protocol.toLowerCase() !== 'ldap:') throw new DirectoryUnavailableError()
  const requestedTransport = (process.env.LDAP_TLS_MODE || '').trim().toLowerCase()
  let transport: LdapConfig['transport']
  if (!requestedTransport) transport = encrypted ? 'ldaps' : 'starttls'
  else if (
    requestedTransport === 'ldaps' ||
    requestedTransport === 'starttls' ||
    requestedTransport === 'none'
  )
    transport = requestedTransport
  else throw new DirectoryUnavailableError()
  if (encrypted !== (transport === 'ldaps')) throw new DirectoryUnavailableError()
  // Explicit host:port only; never infer defaults or accept URLs with
  // credentials, paths, queries or fragments.
  if (!url.port) throw new DirectoryUnavailableError()
  const port = Number(url.port)
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new DirectoryUnavailableError()
  let host = url.hostname
  if (host.startsWith('[') && host.endsWith(']')) host = host.slice(1, -1)
  if (!host || url.username || url.password) throw new DirectoryUnavailableError()
  if ((url.pathname && url.pathname !== '/') || url.search || url.hash)
    throw new DirectoryUnavailableError()

  if (!domain || !DOMAIN_RE.test(domain)) throw new DirectoryUnavailableError()
  if (!baseDN || !bindDN || !bindPassword || (accessMode === 'group' && !groupDN))
    throw new DirectoryUnavailableError()
  // All-user access needs an actual service DN, not a login alias. Resolve it
  // below and let AD compare distinguishedName values using its own DN rules.
  if (accessMode === 'all' && !/^[A-Za-z][A-Za-z0-9.-]*\s*=/.test(bindDN))
    throw new DirectoryUnavailableError()

  let ca: Buffer | undefined
  if (caFile && transport !== 'none') {
    try {
      ca = readFileSync(caFile)
    } catch {
      throw new DirectoryUnavailableError()
    }
    if (ca.length === 0) throw new DirectoryUnavailableError()
  }

  const hostPart = host.includes(':') ? `[${host}]` : host
  return {
    ldapUrl: `${encrypted ? 'ldaps' : 'ldap'}://${hostPart}:${port}`,
    host,
    transport,
    domain,
    baseDN,
    bindDN,
    bindPassword,
    groupDN,
    accessMode,
    ca,
  }
}

/** RFC 4515 filter escaping (only `* ( ) \ NUL` are special in values). */
function escapeFilter(value: string): string {
  return value.replace(/[\0*()\\]/g, (ch) => {
    switch (ch) {
      case '\0':
        return '\\00'
      case '*':
        return '\\2a'
      case '(':
        return '\\28'
      case ')':
        return '\\29'
      default:
        return '\\5c'
    }
  })
}

/**
 * Accepts a bare logon name, `DOMAIN\user` or `user@domain` and returns the
 * account name, but only when the domain part (if any) matches the single
 * configured domain. Anything else is denied, never resolved.
 */
function normalizeAccount(login: string, domain: string): string {
  const value = login.trim()
  let account = value
  const backslash = value.indexOf('\\')
  if (backslash >= 0) {
    const userDomain = value.slice(0, backslash)
    account = value.slice(backslash + 1)
    if (!userDomain || userDomain.toLowerCase() !== domain.toLowerCase())
      throw new AuthDeniedError()
  } else {
    const at = value.lastIndexOf('@')
    if (at >= 0) {
      account = value.slice(0, at)
      const userDomain = value.slice(at + 1)
      if (!userDomain || userDomain.toLowerCase() !== domain.toLowerCase())
        throw new AuthDeniedError()
    }
  }
  if (!ACCOUNT_RE.test(account)) throw new AuthDeniedError()
  return account
}

function firstValue(value: Entry[string] | undefined): string | undefined {
  if (value === undefined) return undefined
  const item = Array.isArray(value) ? value[0] : value
  if (item === undefined) return undefined
  if (typeof item === 'string' || typeof item === 'number') return String(item)
  if (Buffer.isBuffer(item)) return item.toString('utf8')
  return undefined
}

// ldapts exposes the code on its error; accept either shape so a bind
// failure is never misclassified when only the code survives.
function isInvalidCredentials(error: unknown): boolean {
  if (error instanceof InvalidCredentialsError) return true
  if (error !== null && typeof error === 'object' && 'code' in error) return error.code === 49
  return false
}

/**
 * Fail-closed evaluation of AD account status: disabled, locked out or
 * expired accounts are denied. Missing/unparseable status attributes are
 * denied too (the service account must be able to read them).
 */
function accountStatusAllows(entry: Entry): boolean {
  const rawUac = firstValue(entry['userAccountControl'])
  if (rawUac === undefined) return false
  const uac = Number(rawUac)
  const computed = Number(firstValue(entry['msDS-User-Account-Control-Computed']))
  if (!Number.isSafeInteger(uac) || !Number.isSafeInteger(computed)) return false
  if ((uac & UF_ACCOUNTDISABLE) !== 0 || ((uac | computed) & UF_LOCKOUT) !== 0) return false

  const rawExpires = firstValue(entry['accountExpires'])
  if (rawExpires === undefined) return false
  let expires: bigint
  try {
    expires = BigInt(rawExpires)
  } catch {
    return false
  }
  if (expires !== 0n && expires !== NEVER_EXPIRES_FILETIME) {
    const expiresMs = expires / 10000n - FILETIME_TO_MS_OFFSET
    if (expiresMs <= BigInt(Date.now())) return false
  }
  return true
}

function buildTlsOptions(cfg: LdapConfig): tls.ConnectionOptions {
  const options: tls.ConnectionOptions = {
    minVersion: 'TLSv1.2',
    rejectUnauthorized: true,
    // Bound to the configured host (not the socket peer name) so hostname
    // verification also holds for STARTTLS upgrades and IP literals.
    checkServerIdentity: (_servername: string, cert: tls.PeerCertificate) =>
      tls.checkServerIdentity(cfg.host, cert),
  }
  if (cfg.ca) options.ca = [cfg.ca]
  // SNI servername must be a DNS name, never an IP literal.
  if (net.isIP(cfg.host) === 0) options.servername = cfg.host
  return options
}

function createClient(cfg: LdapConfig): Client {
  // ldapts transparently reconnects a dropped socket, losing the STARTTLS
  // upgrade. Pin each client to one TCP connection (+ the single TLS
  // upgrade) so a reconnect fails closed instead of sending credentials
  // in the clear. Never enable autoRebind here.
  let plainUsed = false
  let secureUsed = false
  // Overload shapes differ from net/tls.connect signatures, so the guarded
  // wrappers need one unchecked cast each at this named boundary (ldapts
  // accepts several call shapes; inputs here are ours, never remote data).
  const singlePlainConnection = (...args: unknown[]): net.Socket => {
    if (plainUsed) throw new DirectoryUnavailableError()
    plainUsed = true
    // Reflect preserves every overload shape ldapts may use (port/host,
    // options, existing socket) while the guard pins the client to one.
    return Reflect.apply(net.connect, net, args)
  }
  const singleSecureConnection = (...args: unknown[]): tls.TLSSocket => {
    if (secureUsed) throw new DirectoryUnavailableError()
    secureUsed = true
    const socket: tls.TLSSocket = Reflect.apply(tls.connect, tls, args)
    const deadline = setTimeout(
      () => socket.destroy(new DirectoryUnavailableError()),
      CONNECT_TIMEOUT_MS,
    )
    deadline.unref()
    const clearDeadline = () => clearTimeout(deadline)
    socket.once('secureConnect', clearDeadline)
    socket.once('close', clearDeadline)
    socket.once('error', clearDeadline)
    return socket
  }
  const createConnection: NonNullable<ClientOptions['createConnection']> =
    singlePlainConnection as NonNullable<ClientOptions['createConnection']>
  const createSecureConnection: NonNullable<ClientOptions['createSecureConnection']> =
    singleSecureConnection as NonNullable<ClientOptions['createSecureConnection']>
  const options: ClientOptions = {
    url: cfg.ldapUrl,
    timeout: OPERATION_TIMEOUT_MS,
    connectTimeout: CONNECT_TIMEOUT_MS,
    createConnection,
    createSecureConnection,
  }
  // NB: tlsOptions must stay absent for ldap:// — any defined value flips
  // ldapts into direct-TLS mode and would skip STARTTLS.
  if (cfg.transport === 'ldaps') options.tlsOptions = buildTlsOptions(cfg)
  return new Client(options)
}

async function prepareLdapConnection(client: Client, cfg: LdapConfig): Promise<void> {
  // No implicit downgrade: only explicit none bypasses STARTTLS on ldap://.
  if (cfg.transport !== 'starttls') return
  try {
    await client.startTLS(buildTlsOptions(cfg))
  } catch {
    throw new DirectoryUnavailableError()
  }
}

async function closeQuietly(client: Pick<Client, 'unbind'>): Promise<void> {
  try {
    await client.unbind()
  } catch {
    // Never surface directory errors; the connection is already torn down.
  }
}

async function bindService(client: Client, cfg: LdapConfig): Promise<void> {
  try {
    await client.bind(cfg.bindDN, cfg.bindPassword)
  } catch {
    // Service bind failure is a server/config problem, never "bad login".
    throw new DirectoryUnavailableError()
  }
}

async function runSearch(
  client: Client,
  baseDN: string,
  scope: 'base' | 'sub',
  filter: string,
  attributes: string[],
  sizeLimit: number,
): Promise<Entry[]> {
  try {
    const result = await client.search(baseDN, {
      scope,
      derefAliases: 'never',
      filter,
      attributes,
      sizeLimit,
      timeLimit: SEARCH_TIME_LIMIT_S,
    })
    return result.searchEntries
  } catch {
    throw new DirectoryUnavailableError()
  }
}

/** Nested (transitive) group membership via the AD chain matching rule. */
async function isGroupMember(client: Client, cfg: LdapConfig, userDN: string): Promise<boolean> {
  const entries = await runSearch(
    client,
    userDN,
    'base',
    `(memberOf:${NESTED_GROUP_OID}:=${escapeFilter(cfg.groupDN)})`,
    ['1.1'],
    1,
  )
  return entries.length > 0
}

/** Exclude the search identity in AD, including equivalent DN spellings. */
async function allAccessExclusion(client: Client, cfg: LdapConfig): Promise<string> {
  const entries = await runSearch(client, cfg.bindDN, 'base', '(objectClass=user)', ['1.1'], 1)
  const serviceDN = entries[0]?.dn
  if (entries.length !== 1 || !serviceDN) throw new DirectoryUnavailableError()
  return `(!(distinguishedName=${escapeFilter(serviceDN)}))`
}

async function serviceClient(cfg: LdapConfig): Promise<Client> {
  const client = createClient(cfg)
  try {
    await prepareLdapConnection(client, cfg)
    await bindService(client, cfg)
    return client
  } catch (error: unknown) {
    await closeQuietly(client)
    throw error
  }
}

/**
 * Authenticate a login (bare name, `DOMAIN\user` or `user@domain`) with an AD
 * password. Returns the directory DN and account name. Throws
 * AuthDeniedError for any credential/authorization failure and
 * DirectoryUnavailableError when the directory cannot be reached, verified
 * or correctly configured. Never reveals which step failed.
 */
export async function authenticateLdap(
  login: string,
  password: string,
): Promise<{ dn: string; account: string }> {
  if (typeof login !== 'string' || typeof password !== 'string') throw new AuthDeniedError()
  if (!login.trim() || !password) throw new AuthDeniedError()
  const cfg = loadConfig()
  const account = normalizeAccount(login, cfg.domain)

  // Service connection: locate exactly one enabled account.
  const service = createClient(cfg)
  let user: Client | undefined
  try {
    await prepareLdapConnection(service, cfg)
    await bindService(service, cfg)
    const exclusion = cfg.accessMode === 'all' ? await allAccessExclusion(service, cfg) : ''
    const entries = await runSearch(
      service,
      cfg.baseDN,
      'sub',
      `(&(objectClass=user)(sAMAccountName=${escapeFilter(account)})${exclusion})`,
      [
        'sAMAccountName',
        'userAccountControl',
        'msDS-User-Account-Control-Computed',
        'accountExpires',
      ],
      USER_SEARCH_SIZE_LIMIT,
    )
    if (entries.length !== 1) throw new AuthDeniedError()
    const entry: Entry | undefined = entries[entries.length - 1]
    if (!entry || !entry.dn || !accountStatusAllows(entry)) throw new AuthDeniedError()
    const userDN = entry.dn
    user = createClient(cfg)
    await prepareLdapConnection(user, cfg)
    try {
      await user.bind(userDN, password)
    } catch (error: unknown) {
      if (isInvalidCredentials(error)) throw new AuthDeniedError()
      throw new DirectoryUnavailableError()
    }
    if (cfg.accessMode === 'group' && !(await isGroupMember(service, cfg, userDN)))
      throw new AuthDeniedError()
    return { dn: userDN, account: account.toLowerCase() }
  } catch (error: unknown) {
    if (error instanceof AuthDeniedError) throw error
    throw new DirectoryUnavailableError()
  } finally {
    if (user) await closeQuietly(user)
    await closeQuietly(service)
  }
}

/**
 * Recheck authorization for a known directory DN via a fresh service bind
 * (no user password involved). Returns false for unknown, disabled, locked,
 * expired or policy-denied accounts. All-user access remains scoped to Base DN
 * and excludes the search identity; group access requires nested membership.
 * Throws DirectoryUnavailableError when the directory itself cannot be used.
 */
export async function checkLdapAccess(dn: string): Promise<boolean> {
  if (typeof dn !== 'string' || !dn.trim()) throw new AuthDeniedError()
  const userDN = dn.trim()
  const cfg = loadConfig()
  const client = await serviceClient(cfg).catch((error: unknown) => {
    if (error instanceof AuthDeniedError) throw error
    throw new DirectoryUnavailableError()
  })
  try {
    const exclusion = cfg.accessMode === 'all' ? await allAccessExclusion(client, cfg) : ''
    const statuses = await runSearch(
      client,
      cfg.accessMode === 'all' ? cfg.baseDN : userDN,
      cfg.accessMode === 'all' ? 'sub' : 'base',
      cfg.accessMode === 'all'
        ? `(&(objectClass=user)(distinguishedName=${escapeFilter(userDN)})${exclusion})`
        : '(objectClass=*)',
      ['userAccountControl', 'msDS-User-Account-Control-Computed', 'accountExpires'],
      1,
    )
    if (statuses.length !== 1) return false
    const status: Entry | undefined = statuses[0]
    if (!status || !accountStatusAllows(status)) return false
    return cfg.accessMode === 'all' || (await isGroupMember(client, cfg, userDN))
  } finally {
    await closeQuietly(client)
  }
}
