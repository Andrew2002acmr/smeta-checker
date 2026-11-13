import { describe, expect, it } from 'vitest'
import { backendError } from '../../app/composables/useBackend'

describe('service error envelopes', () => {
  it('preserves the public Nitro authentication error despite its boolean error field', () => {
    expect(
      backendError({
        error: true,
        statusCode: 503,
        statusMessage: 'service unavailable',
        message: 'service unavailable',
      }),
    ).toBe('service unavailable')
  })
  it('retains structured Python API errors', () => {
    expect(
      backendError({ error: { message: 'document rejected', code: 'invalid_document' } }),
    ).toBe('document rejected')
  })
})
