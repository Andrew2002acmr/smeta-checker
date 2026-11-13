import * as Sentry from '@sentry/nuxt'

const dsn = process.env.NUXT_PUBLIC_SENTRY_DSN || process.env.GLITCHTIP_DSN || ''

if (dsn) {
  Sentry.init({
    dsn,
    environment: process.env.SENTRY_ENVIRONMENT || 'production',
    tracesSampleRate: 0.05,
  })
}
