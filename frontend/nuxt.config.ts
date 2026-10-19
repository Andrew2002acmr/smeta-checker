export default defineNuxtConfig({
  compatibilityDate: '2026-10-19',
  ssr: false,
  modules: ['@nuxt/ui'],
  css: ['~/assets/css/theme.css', '~/assets/css/workspace.css'],
  devtools: { enabled: false },
  ui: { fonts: false },
  typescript: { strict: true, typeCheck: false },
  runtimeConfig: { backendUrl: 'http://127.0.0.1:8100', backendToken: '' },
  app: {
    // Separate build assets so an open local stand cannot reuse an older CSS dependency map.
    buildAssetsDir: '/_nuxt/' + Date.now().toString(36) + '/',
    head: { title: 'КДФП · Проверка ВОР', htmlAttrs: { lang: 'ru' } },
  },
  nitro: { preset: 'node-server' },
  vite: { optimizeDeps: { exclude: ['pdfjs-dist'] } },
})
