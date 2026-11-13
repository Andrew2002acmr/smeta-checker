import { request } from '@playwright/test'
import type { FullConfig } from '@playwright/test'

export default async function setup(config: FullConfig) {
  const baseURL = config.projects[0]?.use.baseURL
  const login = process.env.WORKSPACE_LOGIN
  const password = process.env.WORKSPACE_PASSWORD
  if (!baseURL || !login || !password)
    throw new Error('E2E requires baseURL and isolated WORKSPACE_LOGIN/WORKSPACE_PASSWORD')
  const client = await request.newContext({ baseURL })
  try {
    const reply = await client.post('/api/session', {
      headers: { Origin: baseURL },
      data: { login, password },
    })
    if (reply.status() !== 200)
      throw new Error(`Isolated workspace login returned HTTP ${reply.status()}`)
    // Environment-only synthetic session: reused even if Playwright replaces a failed worker.
    process.env.SMETA_E2E_COOKIES = JSON.stringify((await client.storageState()).cookies)
  } catch (error) {
    await client.dispose()
    throw error
  }
  return async () => {
    delete process.env.SMETA_E2E_COOKIES
    try {
      await client.delete('/api/session', { headers: { Origin: baseURL } })
    } finally {
      await client.dispose()
    }
  }
}
