import { defineVitestConfig } from '@nuxt/test-utils/config'
export default defineVitestConfig({
  test: {
    include: ['tests/unit/**/*.test.ts'],
    environment: 'nuxt',
    environmentOptions: { nuxt: { domEnvironment: 'happy-dom' } },
  },
})
