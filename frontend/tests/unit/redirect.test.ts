import { describe, expect, it } from 'vitest'
import { safeInternalRedirect } from '../../app/utils/safeInternalRedirect'

describe('login return path', () => {
  it('preserves internal page query and fragment', () => {
    expect(safeInternalRedirect('/projects/42?view=docs#revision')).toBe(
      '/projects/42?view=docs#revision',
    )
  })
  it.each([
    '//evil.test',
    '/\\evil.test',
    'https://evil.test',
    '/\nevil.test',
    '/login?back=//evil.test',
    undefined,
    ['/projects'],
  ])('rejects unsafe target %s', (value) => {
    expect(safeInternalRedirect(value)).toBe('/')
  })
})
