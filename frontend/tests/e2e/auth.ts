import { test as base, expect } from '@playwright/test'

export const test = base.extend({
  page: async ({ page, context }, use) => {
    const cookies = process.env.SMETA_E2E_COOKIES
    if (!cookies) throw new Error('E2E authentication setup did not run')
    await context.addCookies(JSON.parse(cookies))
    await use(page)
  },
})

export { expect }
