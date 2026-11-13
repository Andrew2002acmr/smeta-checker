// Run inside the workspace container. Credentials come only from its environment.
const required = ['WORKSPACE_TEST_URL', 'LDAP_TEST_LOGIN', 'LDAP_TEST_PASSWORD']
for (const key of required) {
  if (!process.env[key]) throw new Error(`Missing ${key}`)
}
const target = new URL(process.env.WORKSPACE_TEST_URL)
if (
  target.protocol !== 'https:' ||
  target.pathname !== '/' ||
  target.search ||
  target.hash ||
  target.username ||
  target.password
)
  throw new Error('WORKSPACE_TEST_URL must be an HTTPS origin')
let cookie = ''
async function request(path, options = {}) {
  return fetch(new URL(path, target), {
    ...options,
    headers: { Origin: target.origin, Cookie: cookie, ...options.headers },
    redirect: 'error',
    signal: AbortSignal.timeout(30000),
  })
}
function requireStatus(response, status, step) {
  if (response.status !== status)
    throw new Error(`${step}: HTTP ${response.status}, expected ${status}`)
}
try {
  const login = await request('/api/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      login: process.env.LDAP_TEST_LOGIN,
      password: process.env.LDAP_TEST_PASSWORD,
    }),
  })
  requireStatus(login, 200, 'Login')
  const header = login.headers.getSetCookie().find((value) => value.startsWith('kdfp_workspace='))
  if (
    !header ||
    !/;\s*HttpOnly/i.test(header) ||
    !/;\s*Secure/i.test(header) ||
    !/;\s*SameSite=Strict/i.test(header)
  )
    throw new Error('Missing secure session cookie')
  cookie = header.split(';', 1)[0]
  requireStatus(await request('/api/session'), 200, 'Session')
  requireStatus(await request('/'), 200, 'Workspace page')
  requireStatus(await request('/api/v1/projects'), 200, 'Protected API')
  requireStatus(await request('/api/session', { method: 'DELETE' }), 200, 'Logout')
  // Deliberately replay the old cookie: logout must revoke server state, not just clear the browser.
  requireStatus(await request('/api/session'), 401, 'Revoked session')
  requireStatus(await request('/api/v1/projects'), 401, 'API after logout')
  requireStatus(
    await request('/api/v1/documents/verify-access/content'),
    401,
    'Download after logout',
  )
  console.log('AD acceptance passed: login, page, API, logout, old-cookie and download denial')
} finally {
  if (cookie) await request('/api/session', { method: 'DELETE' }).catch(() => {})
}
