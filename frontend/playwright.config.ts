import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './tests/e2e',
  globalSetup: './tests/e2e/setup.ts',
  use: {
    baseURL: process.env.FRONTEND_URL || 'http://127.0.0.1:3000',
    headless: true,
    screenshot: 'only-on-failure',
  },
  workers: 1,
  reporter: 'list',
})
