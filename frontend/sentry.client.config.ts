import * as Sentry from '@sentry/nuxt'

const dsn = useRuntimeConfig().public.sentryDsn

if (dsn) {
  Sentry.init({
    dsn,
    environment: import.meta.env.NUXT_PUBLIC_SENTRY_ENVIRONMENT || 'production',
    tracesSampleRate: 0.05,
    beforeSend(event) {
      // Avoid leaking auth headers / tokens from failed fetches.
      if (event.request?.headers) {
        for (const key of Object.keys(event.request.headers)) {
          if (/authorization|cookie|token|password|secret/i.test(key)) {
            event.request.headers[key] = '[redacted]'
          }
        }
      }
      return event
    },
  })
}
