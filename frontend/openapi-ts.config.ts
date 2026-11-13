import { defineConfig } from '@hey-api/openapi-ts'
export default defineConfig({
  input: './openapi.json',
  output: './app/generated',
  plugins: [
    '@hey-api/typescript',
    '@hey-api/client-fetch',
    '@hey-api/sdk',
    { name: 'zod', compatibilityVersion: 4 },
  ],
})
